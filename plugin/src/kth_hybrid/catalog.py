"""判据目录：正常运行读取包内固定数据，批准wheel只作显式离线参考。

纪律（工作仓自包含合同 v1）：
- 包内工件来自批准wheel六个getter的机械冻结，读取前校验固定字节hash。
- 正常入口不读wheel、不启动子进程；缺失或损坏不回退到旧目录。
- 显式离线提取工具仍双重核验wheel SHA256，不是应用运行路径。
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

# 源码仓内的参考原件，不属于安装包的运行依赖。
APPROVED_WHEEL = (
    Path(__file__).resolve().parent.parent.parent.parent / ".local" / "reference"
    / "approved-baseline" / "oracle.whl"
)
APPROVED_WHEEL_SHA256 = (
    "2c49050858555ebb063a7b82177ee43e7caf7d2b93e2319998d7d0b047a471fe"
)
APPROVED_CATALOG = Path(__file__).with_name("data") / "approved_catalog.v1.json"
APPROVED_CATALOG_SHA256 = (
    "abb21b0e5ff7aae2bf39f9149f3139ea017347ef1875c885da9a9a9e516d7f04"
)


def load_approved_catalog(path: Path | None = None) -> dict[str, Any]:
    """读取独立的新对象；不接受自报身份、不回退或重建损坏工件。"""
    payload = (Path(path) if path is not None else APPROVED_CATALOG).read_bytes()
    if hashlib.sha256(payload).hexdigest() != APPROVED_CATALOG_SHA256:
        raise RuntimeError("批准catalog固定工件身份不符")
    return json.loads(payload)

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
    """兼容入口：无路径时加载包内工件；仅显式路径才执行离线参考提取。

    返回结构：
    ``{"schema_version", "wheel_sha256", "generated_by", "dimensions": {...},
    "totals": {"criteria": 180}}``；每个维度含 registry 原文、判据平铺列表与
    code_pointer（wheel 内模块与 getter）。
    """
    if wheel_path is None:
        if expected_sha256 != APPROVED_WHEEL_SHA256:
            raise RuntimeError("批准wheel身份不符：固定catalog不能换方法身份")
        return load_approved_catalog()
    wheel = Path(wheel_path)
    if wheel_sha256(wheel) != expected_sha256:
        raise RuntimeError(f"批准 wheel 身份不符：{wheel}")
    payload = json.dumps(_DIMENSION_GETTERS)
    with isolated_wheel_probe():
        result = subprocess.run(
            [sys.executable, "-I", "-B", "-X", "utf8", "-c", _PROBE_SCRIPT,
             str(wheel), expected_sha256, payload],
            capture_output=True,
            text=True,
            encoding="utf-8",
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
