"""判据目录：从批准 wheel 的六个 registry getter 机械提取判据索引。

纪律（v3 计划 T02 / 开工指令 §5）：
- 批准 wheel 是唯一执行 oracle，但**不是**本产品的运行时依赖：提取在隔离子进程
  （``python -I``）中完成，wheel 只进入该子进程的 ``sys.path``。
- 提取前在父进程与子进程双重核验 wheel SHA-256；不一致立即失败，不降级。
- 不重新发明 criterion：条目、级别、dispositions、na_policy 等字段原样保留。
- 180 条只是批准 wheel 当前实现的条目，不是完整 KTH 判据全集。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

APPROVED_WHEEL = Path(
    "D:/UGit/sunny-skills/plugins/kth-irl-evaluator/runtime/"
    "kth_irl_evaluator-0.1.5-py3-none-any.whl"
)
APPROVED_WHEEL_SHA256 = (
    "2c49050858555ebb063a7b82177ee43e7caf7d2b93e2319998d7d0b047a471fe"
)

# 六个 getter 的机械定位（维度 → (wheel 内模块, getter 函数)）。
_DIMENSION_GETTERS: dict[str, tuple[str, str]] = {
    "CRL": ("kth_irl.v1.crl_vertical", "get_crl_registry"),
    "BRL": ("kth_irl.v1.brl_vertical", "get_brl_registry"),
    "TRL": ("kth_irl.v1.trl_vertical", "get_trl_registry"),
    "TMRL": ("kth_irl.v1.tmrl_vertical", "get_tmrl_registry"),
    "IPRL": ("kth_irl.v1.iprl_vertical", "get_iprl_registry"),
    "FRL": ("kth_irl.v1.frl_vertical", "get_frl_registry"),
}

# 子进程探针豁免标志：conftest 的执行边界审计钩子读取。
_PROBE_ACTIVE = False


@contextlib.contextmanager
def isolated_wheel_probe():
    """声明"当前正在执行批准 wheel 的隔离子进程探针"。

    执行边界默认拒绝 ``subprocess.Popen``；仅此上下文内放行（且仅放行
    ``sys.executable`` + ``-I``），用于 catalog 提取。Provider/模型调用没有
    也不可获得此豁免。
    """
    global _PROBE_ACTIVE
    if _PROBE_ACTIVE:
        yield
        return
    _PROBE_ACTIVE = True
    try:
        yield
    finally:
        _PROBE_ACTIVE = False


def probe_active() -> bool:
    return _PROBE_ACTIVE


def wheel_sha256(path: Path = APPROVED_WHEEL) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


_PROBE_SCRIPT = """
import hashlib, json, sys
sys.dont_write_bytecode = True
wheel, expected, getters_json = sys.argv[1], sys.argv[2], sys.argv[3]
digest = hashlib.sha256()
with open(wheel, "rb") as handle:
    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
        digest.update(chunk)
if digest.hexdigest() != expected:
    print(json.dumps({"error": "wheel_sha256_mismatch", "actual": digest.hexdigest()}))
    sys.exit(3)
sys.path.insert(0, wheel)
getters = json.loads(getters_json)
out = {}
for dimension, (module, func) in getters.items():
    namespace = __import__(module, fromlist=[func])
    registry = getattr(namespace, func)()
    dispositions = getattr(namespace, "_DISPOSITIONS", None)
    out[dimension] = {
        "module": module,
        "getter": func,
        "dispositions": sorted(dispositions) if dispositions is not None else None,
        "registry": registry,
    }
print(json.dumps({"wheel_sha256": digest.hexdigest(), "dimensions": out},
                 ensure_ascii=False))
"""


def build_catalog_from_wheel(
    wheel_path: Path | None = None, *, expected_sha256: str = APPROVED_WHEEL_SHA256
) -> dict[str, Any]:
    """在隔离子进程中提取六个 registry，返回判据目录（纯数据，无 wheel 依赖）。

    返回结构：
    ``{"schema_version", "wheel_sha256", "generated_by", "dimensions": {...},
    "totals": {"criteria": 180}}``；每个维度含 registry 原文、判据平铺列表与
    code_pointer（wheel 内模块与 getter）。
    """
    wheel = Path(wheel_path) if wheel_path else APPROVED_WHEEL
    if wheel_sha256(wheel) != expected_sha256:
        raise RuntimeError(f"批准 wheel 身份不符：{wheel}")
    payload = json.dumps(_DIMENSION_GETTERS)
    with isolated_wheel_probe():
        result = subprocess.run(
            [sys.executable, "-I", "-c", _PROBE_SCRIPT, str(wheel), expected_sha256, payload],
            capture_output=True,
            text=True,
            timeout=120,
        )
    if result.returncode != 0:
        raise RuntimeError(
            f"wheel 探针子进程失败（退出码 {result.returncode}）：{result.stderr[-2000:]}"
        )
    probe = json.loads(result.stdout)
    if probe.get("error") or probe.get("wheel_sha256") != expected_sha256:
        raise RuntimeError("wheel 探针报告身份不一致")

    dimensions: dict[str, Any] = {}
    total = 0
    for dimension, info in probe["dimensions"].items():
        registry = info["registry"]
        criteria = registry.get("criteria", [])
        total += len(criteria)
        # dispositions 来自各 vertical 模块的 _DISPOSITIONS 常量（机械提取）；
        # registry dict 本身不内嵌该列表。模块缺该常量时为 None，登记为缺口，不伪造。
        dispositions = info.get("dispositions")
        if dispositions is None:
            raise RuntimeError(f"{dimension} 模块缺少 _DISPOSITIONS 常量，判据状态无法机械提取")
        dimensions[dimension] = {
            "module": info["module"],
            "getter": info["getter"],
            "code_pointer": f"{info['module']}::{info['getter']}()",
            "criteria_count": len(criteria),
            "levels_supported": registry.get("levels_supported"),
            "dispositions": dispositions,
            "registry": registry,
        }
    return {
        "schema_version": "kth-hybrid.catalog.v1",
        "wheel_sha256": expected_sha256,
        "generated_by": f"{sys.executable} -I 子进程探针",
        "dimensions": dimensions,
        "totals": {"criteria": total},
    }


def _dispositions_of(registry: dict[str, Any], dimension: str) -> list[str] | None:
    """兼容保留：从 registry 原文取 dispositions；未显式携带时返回 None。

    实际提取路径是各 vertical 模块的 ``_DISPOSITIONS`` 常量（见
    ``build_catalog_from_wheel``）。
    """
    value = registry.get("dispositions")
    if isinstance(value, list):
        return list(value)
    return None


def flatten_criteria(catalog: dict[str, Any]) -> list[dict[str, Any]]:
    """平铺 180 条判据，保留维度与级别。"""
    rows: list[dict[str, Any]] = []
    for dimension, info in catalog["dimensions"].items():
        for criterion in info["registry"].get("criteria", []):
            rows.append({"dimension": dimension, **criterion})
    return rows
