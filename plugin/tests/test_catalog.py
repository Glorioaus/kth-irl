"""T02：判据目录测试——维度计数、CRL 1–4、FRL N/A 字段原样保留。"""

from __future__ import annotations

from pathlib import Path

import pytest

from kth_hybrid import catalog


@pytest.fixture(scope="module")
def built_catalog() -> dict:
    return catalog.build_catalog_from_wheel()


def test_wheel_identity_verified_before_probe():
    assert catalog.wheel_sha256() == catalog.APPROVED_WHEEL_SHA256


def test_dimension_counts_are_13_36_25_38_32_36(built_catalog):
    expected = {"CRL": 13, "BRL": 36, "TRL": 25, "TMRL": 38, "IPRL": 32, "FRL": 36}
    for dimension, count in expected.items():
        info = built_catalog["dimensions"][dimension]
        assert info["criteria_count"] == count, dimension
    assert built_catalog["totals"]["criteria"] == 180


def test_crl_levels_capped_at_1_to_4(built_catalog):
    crl = built_catalog["dimensions"]["CRL"]
    assert crl["levels_supported"] == [1, 2, 3, 4]
    levels = {c["level"] for c in crl["registry"]["criteria"]}
    assert levels <= {1, 2, 3, 4}


def test_crl_has_no_native_insufficient(built_catalog):
    crl = built_catalog["dimensions"]["CRL"]
    assert crl["dispositions"] == ["met", "not_applicable", "not_met", "partial"]
    assert "insufficient" not in crl["dispositions"]
    for dimension in ("BRL", "TRL", "TMRL", "IPRL", "FRL"):
        assert "insufficient" in built_catalog["dimensions"][dimension]["dispositions"], dimension


def test_frl_na_policy_fields_preserved(built_catalog):
    frl = built_catalog["dimensions"]["FRL"]["registry"]
    policies = {c["criterion_id"]: c.get("na_policy") for c in frl["criteria"]}
    assert policies["FRL1-NEED"] == "never"
    assert policies["FRL4-PITCH"] == "explicit_no_external_financing_only"
    assert policies["FRL9-PITCH"] == "explicit_no_external_financing_only"
    # 实测（2026-09-08 从批准 wheel 机械提取）：27 行 never、9 行受限 N/A，共 36。
    never = [cid for cid, policy in policies.items() if policy == "never"]
    restricted = [
        cid for cid, policy in policies.items()
        if policy == "explicit_no_external_financing_only"
    ]
    assert len(never) == 27 and len(restricted) == 9


def test_180_is_not_claimed_as_full_kth_set(built_catalog):
    for dimension, info in built_catalog["dimensions"].items():
        boundary = info["registry"].get("claim_boundary", {})
        assert boundary.get("internal_shadow") is True, dimension
        assert boundary.get("official_kth_assessment") is False, dimension


def test_code_pointers_are_mechanical(built_catalog):
    for dimension, info in built_catalog["dimensions"].items():
        assert info["code_pointer"].endswith(
            f"{dimension.lower()}_vertical::get_{dimension.lower()}_registry()"
        )


def test_flatten_keeps_180_rows_with_dimension(built_catalog):
    rows = catalog.flatten_criteria(built_catalog)
    assert len(rows) == 180
    assert {r["dimension"] for r in rows} == {"CRL", "BRL", "TRL", "TMRL", "IPRL", "FRL"}
    assert all({"criterion_id", "level", "text"} <= set(r) for r in rows)


def test_wrong_wheel_hash_fails_closed(tmp_path: Path):
    fake = tmp_path / "fake.whl"
    fake.write_bytes(b"not-a-wheel")
    with pytest.raises(RuntimeError, match="身份不符"):
        catalog.build_catalog_from_wheel(fake, expected_sha256="0" * 64)
