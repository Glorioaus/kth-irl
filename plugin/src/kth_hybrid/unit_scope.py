"""单元候选与用户确认的引用闭包；不判断原文真实性或Evidence资格。"""

from __future__ import annotations

import json
import math

from .agent_host import ProductRejected, canonical, fields, nonempty, timestamp


UNIT_FIELDS = {
    "unit_id", "name", "product", "market", "root_task", "system_boundary",
    "financing_entity", "subject", "disposition", "absorbed_by", "reason", "citations",
}
DISPOSITIONS = {"included", "excluded", "absorbed", "unresolved"}
ACTION_ID = "external_investment.approve_diligence_or_validation"


def _validate_units(units, coverage: dict, read_text) -> list[dict]:
    if not isinstance(units, list) or not 1 <= len(units) <= 64:
        raise ProductRejected("units: 需要1至64条可定位的单元候选")
    segments = {segment["segment_id"] for segment in coverage["segments"]}
    ids = set()
    for unit in units:
        fields(unit, UNIT_FIELDS, label="unit")
        for key in UNIT_FIELDS - {"absorbed_by", "citations"}:
            nonempty(unit[key], key)
        if len(unit["unit_id"]) > 128 or unit["unit_id"] in ids:
            raise ProductRejected("unit_id: ID过长或重复")
        ids.add(unit["unit_id"])
        if unit["disposition"] not in DISPOSITIONS:
            raise ProductRejected("disposition: 未知处置")
        if unit["disposition"] != "absorbed" and unit["absorbed_by"] is not None:
            raise ProductRejected("absorption: 非吸收单元不得指定吸收目标")
        citations = unit["citations"]
        if not isinstance(citations, list) or not 1 <= len(citations) <= 256:
            raise ProductRejected("citation: 候选须有1至256处原文引用")
        for citation in citations:
            fields(citation, {"segment_id", "start", "end", "quote"}, label="citation")
            if citation["segment_id"] not in segments:
                raise ProductRejected("citation: 不属于当前run材料")
            text = read_text(citation["segment_id"])
            start, end = citation["start"], citation["end"]
            if type(start) is not int or type(end) is not int \
                    or not 0 <= start < end <= len(text) \
                    or citation["quote"] != text[start:end] \
                    or not citation["quote"].strip():
                raise ProductRejected("citation: 引文与原文字符定位不一致")
    by_id = {unit["unit_id"]: unit for unit in units}
    for unit in units:
        if unit["disposition"] == "absorbed":
            target = unit["absorbed_by"]
            if not isinstance(target, str) or target == unit["unit_id"] \
                    or target not in by_id \
                    or by_id[target]["disposition"] != "included":
                raise ProductRejected("absorption: 目标须为本组内的纳入单元")
    return units


def validate_candidate(candidate: dict, coverage: dict, read_text) -> dict:
    candidate = json.loads(canonical(candidate))
    fields(candidate, {"coverage_digest", "units"}, label="scope_candidate")
    if candidate["coverage_digest"] != coverage["coverage_digest"]:
        raise ProductRejected("coverage: 候选不属于冻结材料版本")
    _validate_units(candidate["units"], coverage, read_text)
    return candidate


def _validate_action(action) -> None:
    if action is None:
        return
    fields(action, {"action_id", "deadline", "resource_cap", "responsible", "prohibited"},
           label="action")
    if action["action_id"] != ACTION_ID:
        raise ProductRejected("action: 仅支持有限尽调或验证行动")
    timestamp(action["deadline"])
    nonempty(action["responsible"], "action.responsible")
    cap = action["resource_cap"]
    fields(cap, {"amount", "unit"}, label="resource_cap")
    if type(cap["amount"]) not in {int, float} \
            or not math.isfinite(cap["amount"]) or cap["amount"] < 0:
        raise ProductRejected("resource_cap: 需要有限非负上限")
    nonempty(cap["unit"], "resource_cap.unit")
    forbidden = action["prohibited"]
    if not isinstance(forbidden, list) \
            or any(not isinstance(item, str) for item in forbidden) \
            or not {"investment", "payment", "contract"} <= set(forbidden):
        raise ProductRejected("authority: 不得扩大投资/付款/签约权限")


def validate_confirmation(confirmation: dict, candidate: dict,
                          coverage: dict, read_text) -> dict:
    confirmation = json.loads(canonical(confirmation))
    fields(confirmation, {
        "candidate_digest", "actor", "confirmed_at", "subject", "evidence_cutoff",
        "units", "action", "permissions",
    }, label="confirmation")
    if confirmation["candidate_digest"] != candidate["candidate_digest"]:
        raise ProductRejected("candidate_digest: 不是当前冻结候选")
    nonempty(confirmation["actor"], "actor")
    confirmed_at = timestamp(confirmation["confirmed_at"])
    if timestamp(confirmation["evidence_cutoff"]) > confirmed_at:
        raise ProductRejected("evidence_cutoff: 不能把未来作为已发生事实时点")
    subject = confirmation["subject"]
    fields(subject, {"legal_name", "aliases"}, label="subject")
    nonempty(subject["legal_name"], "legal_name")
    if not isinstance(subject["aliases"], list) or len(subject["aliases"]) > 128:
        raise ProductRejected("aliases: 别名须为有界列表")
    for alias in subject["aliases"]:
        nonempty(alias, "alias")
    units = _validate_units(confirmation["units"], coverage, read_text)
    proposed = {unit["unit_id"]: unit for unit in candidate["units"]}
    if {unit["unit_id"] for unit in units} != set(proposed):
        raise ProductRejected("units: 集中确认必须覆盖全部候选ID")
    included = [unit for unit in units if unit["disposition"] == "included"]
    if not included or any(unit["disposition"] == "unresolved" for unit in units):
        raise ProductRejected("unresolved: 未决或无纳入单元，不能冻结专业范围")
    legal_subjects = {subject["legal_name"], *subject["aliases"]}
    if any(unit["subject"] not in legal_subjects for unit in included):
        raise ProductRejected("subject: 纳入单元与确认主体不一致")
    _validate_action(confirmation["action"])
    fields(confirmation["permissions"], {"mode", "external_actions"}, label="permissions")
    if confirmation["permissions"]["mode"] != "offline" \
            or confirmation["permissions"]["external_actions"] is not False:
        raise ProductRejected("authorization: 范围确认不授予真实动作许可")
    return {
        **confirmation,
        "changes": {"units": [
            {"unit_id": unit["unit_id"], "before": proposed[unit["unit_id"]],
             "after": unit}
            for unit in units if unit != proposed[unit["unit_id"]]
        ]},
    }
