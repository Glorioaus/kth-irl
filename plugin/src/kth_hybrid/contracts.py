"""共享合同：常量、身份类型与哈希工具（business-contract-v1 的 R1 子集）。

产品状态与原生状态分列：``insufficient``（证据不足）、``method_unsupported``
（方法范围不支持）、``execution_failed``（执行失败）互不互换；后两者永不映射
为业务 NO（R1 不产生业务值，此处仅锁定枚举以防误用）。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

# Case 阶段与执行状态（阶段名不是业务完成凭证）
CASE_STAGES = ("intake", "evidence", "dimensions", "decision", "report", "delivered")
STAGE_STATES = ("pending", "running", "succeeded", "blocked", "failed")

# 外部任务状态：本地已记录 dispatch 但无持久结果时保守视为 unknown
EXTERNAL_TASK_STATES = (
    "planned",
    "claimed",
    "dispatch_recorded",
    "succeeded",
    "failed",
    "outcome_unknown",
)

# 原生判据处置（批准 wheel 六维 _DISPOSITIONS 的并集；CRL 无 insufficient）
NATIVE_DISPOSITIONS = ("met", "not_met", "partial", "not_applicable", "insufficient")

# 产品侧状态：与原生处置分列，不得混写
PRODUCT_STATUSES = ("succeeded", "insufficient", "method_unsupported", "execution_failed")

# 资格判断状态
QUALIFICATION_STATUSES = ("qualified", "rejected", "needs_review")


@dataclass(frozen=True)
class BlobRef:
    """内容寻址原件引用：不可变、由字节决定。"""

    sha256: str
    byte_length: int


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def is_sha256_hex(value: str) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(c in "0123456789abcdef" for c in value)
    )
