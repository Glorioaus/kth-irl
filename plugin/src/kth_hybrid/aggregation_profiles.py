"""命名六维聚合 profile：仅登记当前批准的确定性组合。"""

from __future__ import annotations

import copy
import json

from .catalog import APPROVED_WHEEL_SHA256
from .contracts import sha256_hex
from .kernels.brl import RULE_VERSION as BRL_RULE_VERSION
from .kernels.crl import RULE_VERSION as CRL_RULE_VERSION
from .kernels.frl import RULE_VERSION as FRL_RULE_VERSION
from .kernels.iprl import RULE_VERSION as IPRL_RULE_VERSION
from .kernels.tmrl import RULE_VERSION as TMRL_RULE_VERSION
from .kernels.trl import RULE_VERSION as TRL_RULE_VERSION

PROFILE_SCHEMA = "kth-hybrid.aggregation-profile.v1"
CURRENT_AGGREGATION_PROFILE_NAME = "kth-local-six-dimension-20260910"


def _content_address(body: dict) -> dict:
    digest = sha256_hex(json.dumps(
        body, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    return {**body, "profile_id": f"AGGPROF::{digest}",
            "profile_digest": digest}


_CURRENT_PROFILE = _content_address({
    "schema_version": PROFILE_SCHEMA,
    "profile_name": CURRENT_AGGREGATION_PROFILE_NAME,
    "catalog_sha256": APPROVED_WHEEL_SHA256,
    "dimensions": {
        "CRL": {
            "rule_version": CRL_RULE_VERSION,
            "result_schema_version": "kth-hybrid.r2a-crl-dimension.v3",
        },
        "BRL": {
            "rule_version": BRL_RULE_VERSION,
            "result_schema_version": "kth-hybrid.dimension-result.v2",
        },
        "TRL": {
            "rule_version": TRL_RULE_VERSION,
            "result_schema_version": "kth-hybrid.dimension-result.v2",
        },
        "IPRL": {
            "rule_version": IPRL_RULE_VERSION,
            "result_schema_version": "kth-hybrid.dimension-result.v2",
        },
        "TMRL": {
            "rule_version": TMRL_RULE_VERSION,
            "result_schema_version": "kth-hybrid.dimension-result.v2",
        },
        "FRL": {
            "rule_version": FRL_RULE_VERSION,
            "result_schema_version": "kth-hybrid.dimension-result.v2",
        },
    },
    "shared_unit_policy": {
        "assessment_unit_dimensions": ["BRL", "TRL", "IPRL", "TMRL"],
        "require_identical_scope_id": True,
        "require_identical_assessment_scope": True,
        "frl_must_reference_shared_assessment_unit": True,
    },
})

CURRENT_AGGREGATION_PROFILE_ID = _CURRENT_PROFILE["profile_id"]
_PROFILE_REGISTRY = {CURRENT_AGGREGATION_PROFILE_ID: _CURRENT_PROFILE}


def get_aggregation_profile(profile_id: str) -> dict:
    """按内容寻址 ID 返回登记 profile 的副本；调用方字典不能充当 profile。"""
    if not isinstance(profile_id, str):
        raise TypeError("aggregation profile_id 必须是已登记的字符串ID")
    profile = _PROFILE_REGISTRY.get(profile_id)
    if profile is None:
        raise ValueError(f"aggregation profile未登记：{profile_id}")
    return copy.deepcopy(profile)
