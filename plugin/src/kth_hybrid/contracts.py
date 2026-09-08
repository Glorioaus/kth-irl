"""共享合同：常量、身份类型与哈希工具（business-contract-v1 的 R1 子集）。

产品状态与原生状态分列：``insufficient``（证据不足）、``method_unsupported``
（方法范围不支持）、``execution_failed``（执行失败）互不互换；后两者永不映射
为业务 NO（R1 不产生业务值，此处仅锁定枚举以防误用）。
"""

from __future__ import annotations

import hashlib
import json
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


def claim_content_digest(claim_row: dict) -> str:
    """主张内容摘要（可从存储行重算：冻结后改动解释/主体/定位即失配）。

    R1.2-C：字段集=真正构成主张内容的全部行字段；摘要存放于
    ``claims.content_digest``，trace 重算比对。
    """
    payload = json.dumps({
        "source_id": claim_row.get("source_id"),
        "locator_kind": claim_row.get("locator_kind"),
        "locator_start": claim_row.get("locator_start"),
        "locator_end": claim_row.get("locator_end"),
        "locator_ref": claim_row.get("locator_ref"),
        "excerpt_sha256": claim_row.get("excerpt_sha256"),
        "excerpt_text": claim_row.get("excerpt_text"),
        "interpretation": claim_row.get("interpretation"),
        "subject_scope": claim_row.get("subject_scope"),
    }, ensure_ascii=False, sort_keys=True)
    return sha256_hex(payload.encode("utf-8"))


def run_input_digest_v3(frozen_inputs: dict, claim_row: dict) -> str:
    """完整输入摘要 v4（R1.3-C：闭包补全；可仅凭存储数据重算）。

    frozen_inputs 新增：source_inputs（实际参与判断的来源字段＋所用时间证据
    修订快照）、qualification_digest、na_proposal 全文（含引用与解析值）、
    mapping（含确认记录摘要）。
    """
    payload = json.dumps({
        "criterion": {k: frozen_inputs.get("criterion", {}).get(k) for k in
                      ("criterion_id", "dimension", "level", "text", "na_policy")},
        "catalog_sha256": frozen_inputs.get("catalog_sha256", ""),
        "approved_ids": frozen_inputs.get("approved_ids", []),
        "rule_version": frozen_inputs.get("rule_version", ""),
        "qualification_version": frozen_inputs.get("qualification_version", ""),
        "case_basis": frozen_inputs.get("case_basis", {}),
        "case_flags": frozen_inputs.get("case_flags", {}),
        "claim_content_digest": claim_content_digest(claim_row),
        "mapping": {k: frozen_inputs.get("mapping", {}).get(k) for k in
                    ("status", "quote_sha256", "quote_start", "quote_end",
                     "confirmation")}
        if isinstance(frozen_inputs.get("mapping"), dict) else None,
        "source_inputs": frozen_inputs.get("source_inputs", {}),
        "qualification_digest": frozen_inputs.get("qualification_digest", ""),
        "na_proposal": frozen_inputs.get("na_proposal"),
    }, ensure_ascii=False, sort_keys=True)
    return sha256_hex(payload.encode("utf-8"))


def qualification_content_digest(qualification_row: dict) -> str:
    """资格记录内容摘要（四类判断依据＋用途＋状态；篡改可检）。"""
    payload = json.dumps({
        "claim_id": qualification_row.get("claim_id"),
        "source_judgment": qualification_row.get("source_judgment"),
        "identity_judgment": qualification_row.get("identity_judgment"),
        "time_judgment": qualification_row.get("time_judgment"),
        "independence_judgment": qualification_row.get("independence_judgment"),
        "allowed_uses": qualification_row.get("allowed_uses"),
        "cannot_prove": qualification_row.get("cannot_prove"),
        "status": qualification_row.get("status"),
    }, ensure_ascii=False, sort_keys=True)
    return sha256_hex(payload.encode("utf-8"))
