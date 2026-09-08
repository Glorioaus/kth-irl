"""R1.3 运行编排 v4：预解析封存证据、确认映射、完整冻结、原子发布。

修复（R1.3 A/B/C）：
- 主体依据/N-A 适用性与 flag 引用由 runner **预解析**到封存原件字段值后
  才进入资格/判据判断（qualification 侧同核）。
- 映射=关键词候选 + ``confirm_mapping`` 留痕确认（否定门控）；candidate
  不构成支持关系。
- 冻结输入 v4 补全实际参与判断的全部内容：来源判断字段＋所用时间证据
  **修订快照**、资格内容摘要、映射＋确认、N/A 提案全文（含引用与解析值）。
- **原子发布**：先验证（``_verify_candidate``，含全部绑定核验）后单条
  INSERT；验证失败/中断时不发布成功（留失败候选），绝不"先提交succeeded
  再trace再改回"。
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Callable

from .contracts import (
    claim_content_digest,
    qualification_content_digest,
    run_input_digest_v3,
    sha256_hex,
)
from .dimensions import CriterionEvaluation, evaluate_criterion
from .intake import extract_docx_paragraphs, extract_pdf_pages
from .journal import Journal
from .kernels import (
    RULE_VERSION,
    confirm_mapping,
    interpret_claim_for_criterion,
)
from .kernels.crl import evaluate_crl_dimension
from .kernels.frl import evaluate_frl_dimension
from .kernels.brl import evaluate_brl_dimension
from .qualification import (
    GapOutcome,
    QualificationOutcome,
    build_qualification_input_view,
    qualify_claim,
    RULE_VERSION as QUALIFICATION_VERSION,
    _same_record_relation,
    resolve_case_field_reference_binding,
    resolve_case_basis_proof_bindings,
    resolve_document_subject_proof_bindings,
    resolve_timezone_rule_binding,
)
from .store import BlobStore, CaseStore

WEIJIU_CASE_BASIS = {
    "subject_legal_name": "微玖（苏州）光电科技有限公司",
    "subject_aliases": ["微玖", "微玖光电"],
    "evidence_cutoff": "2026-08-27T03:02:29Z",
    "subject_source_basis": json.dumps({
        "kind": "field_reference",
        "path": "case:identity-plan.json#/subjects/0/canonical_name_claimed",
        "status": "claimed",
        "note": "封存session身份计划声称的规范主体名（claimed，未工商核验）；"
                "identity-plan.json 已封存于本Case blobs",
    }, ensure_ascii=False),
}


class SimulatedCrash(RuntimeError):
    """注入的模拟崩溃。"""


def qualification_id(claim_id: str) -> str:
    return f"QUALR::{claim_id}"


def result_id(claim_id: str, criterion_id: str) -> str:
    return f"RESR::{claim_id}::{criterion_id}"


def resolve_criterion(criterion_id: str, catalog: dict) -> dict:
    """从可信 catalog 解析判据完整身份（dimension/level/text/na_policy 原样）。"""
    for dim_name, info in catalog["dimensions"].items():
        for criterion in info["registry"].get("criteria", []):
            if criterion["criterion_id"] == criterion_id:
                resolved = {"dimension": dim_name, **criterion}
                resolved.setdefault("levels_supported", info["levels_supported"])
                return resolved
    raise ValueError(
        f"判据 {criterion_id} 不在可信 catalog（批准 wheel）中：不允许评估未登记"
        f"或调用方自报的判据"
    )


def approved_ids_from_catalog(catalog: dict) -> set[str]:
    return {c["criterion_id"] for info in catalog["dimensions"].values()
            for c in info["registry"].get("criteria", [])}


def _resolve_na_proposal(na_proposal, case, blobs):
    """预解析 N/A 适用性与 flag 引用到封存对象字段值（R1.3-B）。"""
    if not isinstance(na_proposal, dict):
        return na_proposal
    # ``*_resolved`` 是系统派生字段。请求中的同名值全部剥离，避免调用方
    # 以 `_resolved=true` 冒充已核验政策；仅保留引用，再从封存对象重建。
    out = {key: value for key, value in na_proposal.items()
           if key not in {"applicability_resolved", "flag_resolved",
                          "subject_resolved"}}
    for ref_key, res_key in (("applicability_ref", "applicability_resolved"),
                             ("flag_ref", "flag_resolved"),
                             ("subject_ref", "subject_resolved")):
        ref = out.get(ref_key)
        if ref is None:
            out[res_key] = {"_resolved": False, "error": f"缺少 {ref_key}"}
            continue
        value, err, binding = resolve_case_field_reference_binding(ref, case, blobs)
        if err:
            out[res_key] = {"_resolved": False, "error": err,
                            "path": ref.get("path") if isinstance(ref, dict) else None}
        else:
            out[res_key] = {"_resolved": True, "value": value,
                            "path": ref.get("path"), "binding": binding}
    resolved = [out.get(key) for key in ("applicability_resolved", "flag_resolved",
                                         "subject_resolved")]
    if all(isinstance(item, dict) and item.get("_resolved") for item in resolved):
        relation_ok, relation_error = _same_record_relation(
            *(item.get("binding") for item in resolved))
        out["relation_valid"] = relation_ok
        out["relation_error"] = None if relation_ok else relation_error
        if relation_ok:
            out["relation"] = {
                "applicability": resolved[0]["binding"],
                "flag": resolved[1]["binding"],
                "subject": resolved[2]["binding"],
            }
    else:
        out["relation_valid"] = False
        out["relation_error"] = "N/A 三个字段未全部解析到封存对象"
    return out


def _controlled_mapping_review(case: CaseStore, review_id, *, basis_version: int,
                               claim_id: str, criterion_id: str,
                               mapping: dict) -> dict | None:
    """运行期只消费已登记的具体复核，不信任 claim_spec 自报确认。"""
    if not isinstance(review_id, str) or not review_id.strip():
        return None
    review = case.get_mapping_review(review_id)
    if review is None:
        return None
    expected = {
        "case_basis_version": basis_version,
        "claim_id": claim_id,
        "criterion_id": criterion_id,
        "quote_sha256": mapping.get("quote_sha256"),
    }
    if any(review.get(field) != value for field, value in expected.items()):
        return None
    if review.get("decision") != "confirmed":
        return None
    return {
        "_controlled": True,
        "review_id": review["review_id"],
        "decision": review["decision"],
        "reviewer": review["reviewer"],
        "review_basis": review["review_basis"],
        "support_scope": review["support_scope"],
        "case_basis_version": review["case_basis_version"],
        "claim_id": review["claim_id"],
        "criterion_id": review["criterion_id"],
        "quote_sha256": review["quote_sha256"],
    }


def run_crl_dimension_slice(case_dir: Path | str, *, catalog: dict,
                            case_basis: dict, scope: str,
                            output_path: Path | str | None = None) -> dict:
    """R2-A：复用R1证据门，验证后原子发布不可变CRL维度结果。"""
    case_dir = Path(case_dir)
    blobs = BlobStore(case_dir / "blobs")
    case = CaseStore(case_dir / "records.sqlite3")
    try:
        basis, version = _effective_case_basis(case, case_basis)
        if scope != basis["subject_legal_name"]:
            raise ValueError("CRL维度scope必须等于冻结Case主体，不能接受任意scope")
        criteria = [dict(row) for row in catalog["dimensions"]["CRL"]["registry"]["criteria"]]
        criterion_ids = {row["criterion_id"] for row in criteria}
        with case.immediate_transaction():
            basis_proofs, basis_error = resolve_case_basis_proof_bindings(basis, case, blobs)
            reviews, rejected, evidence_bindings = [], [], []
            if basis_error or basis_proofs is None:
                rejected.append({"review_id": "__case_basis__",
                                 "reason": f"CaseBasis证明不可核验：{basis_error}"})
                basis_proofs = None
            for row in case.fetch_crl_evidence_reviews(version):
                claim = case.fetch_one("claims", "claim_id", row["claim_id"])
                qual = case.fetch_one("qualifications", "qual_id", qualification_id(row["claim_id"]))
                source = case.fetch_one("sources", "source_id", claim["source_id"]) if claim else None
                reason = None
                if basis_proofs is None:
                    reason = "CaseBasis证明不可核验"
                elif row.get("subject_scope") != scope:
                    reason = "CRL复核subject_scope与维度scope不一致"
                elif row["criterion_id"] not in criterion_ids:
                    reason = "CRL复核指向未登记/非CRL准则"
                elif claim is None or qual is None or qual["status"] != "qualified" or source is None:
                    reason = "主张、来源或资格不可用"
                try:
                    data = blobs.read_bytes(source["blob_sha256"]) if source else b""
                except Exception as exc:
                    data, reason = b"", f"原件不可读：{exc}"
                current_claim_digest = claim_content_digest(claim) if claim else None
                if not reason and (len(data) != source["byte_length"] or
                                   current_claim_digest != claim.get("content_digest")):
                    reason = "来源长度或Claim冻结内容不一致"
                if not reason:
                    from .audit import _excerpt_bytes_for_claim
                    excerpt = _excerpt_bytes_for_claim(blobs, claim, source)
                    if excerpt is None or sha256_hex(excerpt) != claim["excerpt_sha256"]:
                        reason = "定位/投影/摘录hash不符"
                if not reason and claim["excerpt_sha256"] != row["quote_sha256"]:
                    reason = "复核引文hash与封存摘录不一致"
                qualification_view = None
                if not reason:
                    qualification_view, outcome, proof_errors = \
                        build_qualification_input_view(
                        claim, source, qual, basis, case, blobs,
                        same_body_sources=_same_body_occurrence_count(case, source["blob_sha256"]),
                        review_attempt="r2a-dimension-reverify",
                        case_basis_proofs=basis_proofs,
                    )
                    if not isinstance(outcome, QualificationOutcome) or outcome.status != "qualified":
                        reason = "R1资格重核不再qualified"
                    elif proof_errors:
                        reason = "完整资格证明不可核验：" + "；".join(proof_errors)
                binding = {"review": {key: row.get(key) for key in
                           ("review_id", "criterion_id", "claim_id", "quote_sha256",
                            "decision", "findings", "subject_scope", "support_scope",
                            "reviewer", "review_basis")},
                           "claim": claim, "claim_digest": current_claim_digest,
                           "source": source,
                           "qualification_digest": qualification_content_digest(qual) if qual else None,
                           "qualification_view": qualification_view}
                if reason:
                    rejected.append({"review_id": row["review_id"], "reason": reason,
                                     "evidence_binding": binding})
                    continue
                review = {key: row[key] for key in
                          ("review_id", "criterion_id", "claim_id", "decision", "findings",
                           "reviewer", "review_basis", "support_scope")}
                reviews.append(review)
                evidence_bindings.append(binding)
            dimension = evaluate_crl_dimension(criteria, reviews, scope=scope)
            if rejected:
                for item in dimension["criteria"]:
                    item["native_disposition"] = None
                    item["product_status"] = "execution_failed"
                dimension.update(product_status="execution_failed", attained_level=None,
                                 first_unmet_level=None, execution_errors=rejected)
            frozen = {"case_basis": basis, "case_basis_version": version,
                      "case_basis_proofs": basis_proofs,
                      "catalog_sha256": catalog.get("wheel_sha256"), "criteria": criteria,
                      "reviews": reviews, "rejected_reviews": rejected,
                      "evidence_bindings": evidence_bindings, "scope": scope,
                      "rule_version": dimension["rule_version"]}
            digest = sha256_hex(json.dumps(frozen, ensure_ascii=False, sort_keys=True).encode("utf-8"))
            result_id_value = f"CRLR2A::{digest}"
            result = {"schema_version": "kth-hybrid.r2a-crl-dimension.v3",
                      "result_id": result_id_value, "input_digest": digest,
                      "dimension": dimension, "frozen_inputs": frozen,
                      "traceability": {"review_refs": [r["review_id"] for r in reviews],
                                       "claim_refs": sorted({r["claim_id"] for r in reviews})}}
            from .audit import validate_crl_dimension_payload
            publish_errors = validate_crl_dimension_payload(case, blobs, result)
            if publish_errors and dimension["product_status"] != "execution_failed":
                for item in dimension["criteria"]:
                    item["native_disposition"] = None
                    item["product_status"] = "execution_failed"
                dimension.update(
                    product_status="execution_failed", attained_level=None,
                    first_unmet_level=None,
                    execution_errors=[{
                        "review_id": "__publish_validation__",
                        "reason": "发布前完整资格视图核验失败",
                        "broken": publish_errors,
                    }],
                )
                frozen["publish_validation_errors"] = publish_errors
                digest = sha256_hex(json.dumps(
                    frozen, ensure_ascii=False, sort_keys=True).encode("utf-8"))
                result_id_value = f"CRLR2A::{digest}"
                result.update(result_id=result_id_value, input_digest=digest)
            result_bytes = json.dumps(result, ensure_ascii=False, sort_keys=True).encode("utf-8")
            result_ref = blobs.put_bytes(result_bytes)
            existing = case.get_crl_dimension_result(digest)
            if existing is None:
                case.add_crl_dimension_result_in_transaction(
                    result_id_value, input_digest=digest, case_basis_version=version,
                    scope=scope, product_status=dimension["product_status"],
                    result_blob_sha256=result_ref.sha256)
        destination = Path(output_path) if output_path else case_dir / "audit" / "crl-dimension-r2a.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        case.new_run(input_digest=digest)
        return {**result, "output_path": str(destination)}
    finally:
        case.close()


def _resolve_reference_bundle(case: CaseStore, blobs: BlobStore,
                              values_and_refs: list[tuple[str, object, dict]]) -> dict:
    """解析一组必须来自同一封存逻辑记录的字段并冻结实际绑定。"""
    bindings = {}
    resolved = []
    for name, expected, reference in values_and_refs:
        value, error, binding = resolve_case_field_reference_binding(
            reference, case, blobs, expect_value=expected)
        if error or binding is None:
            raise ValueError(f"{name}证明不可核验：{error or '无绑定'}")
        bindings[name] = {**binding, "value": value}
        resolved.append(binding)
    relation_ok, relation_error = _same_record_relation(*resolved)
    if not relation_ok:
        raise ValueError(f"证明字段不属于同一记录：{relation_error}")
    return bindings


def _resolve_financing_entity(case: CaseStore, blobs: BlobStore,
                              value: dict, scope: str) -> dict:
    required = {
        "financing_entity_id", "subject_scope", "assessment_unit_refs",
        "entity_ref", "subject_ref", "assessment_units_ref",
    }
    if not isinstance(value, dict) or not required <= set(value):
        raise ValueError("融资主体输入结构不完整")
    if value["subject_scope"] != scope:
        raise ValueError("融资主体subject_scope与Case主体不一致")
    units = value["assessment_unit_refs"]
    if not isinstance(units, list) or not units or len(units) != len(set(units)):
        raise ValueError("融资主体评估单元必须为非空去重列表")
    proof_bindings = _resolve_reference_bundle(case, blobs, [
        ("entity", value["financing_entity_id"], value["entity_ref"]),
        ("subject", scope, value["subject_ref"]),
        ("assessment_units", units, value["assessment_units_ref"]),
    ])
    return {
        "financing_entity_id": value["financing_entity_id"],
        "subject_scope": scope,
        "assessment_unit_refs": sorted(units),
        "proof_bindings": proof_bindings,
    }


def _resolve_frl_applicability(case: CaseStore, blobs: BlobStore,
                               policy: dict | None, *, scope: str,
                               financing_entity_id: str) -> tuple[dict | None, str | None]:
    if policy is None:
        return None, None
    required = {
        "external_financing_planned_ref", "financing_entity_ref", "subject_ref"}
    if not isinstance(policy, dict) or not required <= set(policy):
        return None, "FRL受限N/A政策结构不完整"
    try:
        bindings = _resolve_reference_bundle(case, blobs, [
            ("external_financing_planned", False,
             policy["external_financing_planned_ref"]),
            ("financing_entity_id", financing_entity_id,
             policy["financing_entity_ref"]),
            ("subject_scope", scope, policy["subject_ref"]),
        ])
    except ValueError as exc:
        return None, str(exc)
    return {
        "external_financing_planned": False,
        "financing_entity_id": financing_entity_id,
        "subject_scope": scope,
        "policy_binding": bindings["external_financing_planned"],
        "proof_bindings": bindings,
    }, None


def _bind_dimension_review(case: CaseStore, blobs: BlobStore, row: dict,
                           basis: dict, basis_proofs: dict,
                           review_fields: tuple[str, ...]) -> tuple[dict, dict, str | None]:
    """建立非CRL维度review的完整资格视图，供后续维度共同复用。"""
    claim = case.fetch_one("claims", "claim_id", row["claim_id"])
    qual = case.fetch_one(
        "qualifications", "qual_id", qualification_id(row["claim_id"]))
    source = case.fetch_one(
        "sources", "source_id", claim["source_id"]) if claim else None
    reason = None
    if claim is None or qual is None or qual["status"] != "qualified" or source is None:
        reason = "主张、来源或资格不可用"
    try:
        data = blobs.read_bytes(source["blob_sha256"]) if source else b""
    except Exception as exc:
        data, reason = b"", f"原件不可读：{exc}"
    current_claim_digest = claim_content_digest(claim) if claim else None
    if not reason and (len(data) != source["byte_length"]
                       or current_claim_digest != claim.get("content_digest")):
        reason = "来源长度或Claim冻结内容不一致"
    if not reason:
        from .audit import _excerpt_bytes_for_claim
        excerpt = _excerpt_bytes_for_claim(blobs, claim, source)
        if excerpt is None or sha256_hex(excerpt) != claim["excerpt_sha256"]:
            reason = "定位/投影/摘录hash不符"
    if not reason and claim["excerpt_sha256"] != row["quote_sha256"]:
        reason = "复核引文hash与封存摘录不一致"
    qualification_view = None
    if not reason:
        qualification_view, outcome, proof_errors = build_qualification_input_view(
            claim, source, qual, basis, case, blobs,
            same_body_sources=_same_body_occurrence_count(
                case, source["blob_sha256"]),
            review_attempt=f"{row['dimension_id'].lower()}-dimension-reverify",
            case_basis_proofs=basis_proofs,
        )
        if not isinstance(outcome, QualificationOutcome) \
                or outcome.status != "qualified":
            reason = "R1资格重核不再qualified"
        elif proof_errors:
            reason = "完整资格证明不可核验：" + "；".join(proof_errors)
    saved_review = {key: row.get(key) for key in review_fields}
    binding = {
        "review": saved_review,
        "claim": claim,
        "claim_digest": current_claim_digest,
        "source": source,
        "qualification_digest": (
            qualification_content_digest(qual) if qual else None),
        "qualification_view": qualification_view,
    }
    review = {key: row[key] for key in review_fields
              if key not in {"quote_sha256", "subject_scope", "scope_id",
                             "dimension_id"}}
    review["financing_entity_id"] = row["scope_id"]
    review["scope_id"] = row["scope_id"]
    return review, binding, reason


def run_frl_dimension_slice(
        case_dir: Path | str, *, catalog: dict, case_basis: dict, scope: str,
        financing_entity: dict, applicability_policy: dict | None = None,
        output_path: Path | str | None = None) -> dict:
    """R2-B候选：按融资主体运行FRL，并复用完整资格视图与不可变发布。"""
    case_dir = Path(case_dir)
    blobs = BlobStore(case_dir / "blobs")
    case = CaseStore(case_dir / "records.sqlite3")
    try:
        basis, version = _effective_case_basis(case, case_basis)
        if scope != basis["subject_legal_name"]:
            raise ValueError("FRL维度scope必须等于冻结Case主体")
        if not isinstance(financing_entity, dict) \
                or financing_entity.get("subject_scope") != scope:
            raise ValueError("融资主体subject_scope与Case主体不一致")
        raw_units = financing_entity.get("assessment_unit_refs")
        if not isinstance(raw_units, list) or not raw_units \
                or len(raw_units) != len(set(raw_units)):
            raise ValueError("融资主体评估单元必须为非空去重列表")
        with case.immediate_transaction():
            basis_proofs, basis_error = resolve_case_basis_proof_bindings(
                basis, case, blobs)
            reviews, rejected, evidence_bindings = [], [], []
            if basis_error or basis_proofs is None:
                rejected.append({
                    "review_id": "__case_basis__",
                    "reason": f"CaseBasis证明不可核验：{basis_error or '无绑定'}",
                })
                basis_proofs = None
            try:
                entity = _resolve_financing_entity(
                    case, blobs, financing_entity, scope)
                entity_error = None
            except ValueError as exc:
                entity_error = str(exc)
                entity = {
                    "financing_entity_id": financing_entity.get(
                        "financing_entity_id") or "__invalid__",
                    "subject_scope": scope,
                    "assessment_unit_refs": sorted(raw_units),
                    "proof_bindings": {},
                }
                rejected.append({
                    "review_id": "__financing_entity__",
                    "reason": f"融资主体证明不可核验：{entity_error}",
                })
            applicability, applicability_error = _resolve_frl_applicability(
                case, blobs, applicability_policy, scope=scope,
                financing_entity_id=entity["financing_entity_id"])
            criteria = [dict(row) for row in
                        catalog["dimensions"]["FRL"]["registry"]["criteria"]]
            criterion_ids = {row["criterion_id"] for row in criteria}
            review_fields = (
                "review_id", "dimension_id", "criterion_id", "claim_id",
                "quote_sha256", "decision", "evidence_class", "findings",
                "subject_scope", "scope_id", "support_scope", "reviewer",
                "review_basis",
            )
            if applicability_error:
                rejected.append({
                    "review_id": "__frl_applicability__",
                    "reason": applicability_error,
                })
            rows = case.fetch_dimension_evidence_reviews(
                "FRL", version, entity["financing_entity_id"])
            for row in rows:
                reason = None
                if basis_proofs is None:
                    reason = "CaseBasis证明不可核验"
                elif entity_error:
                    reason = "融资主体证明不可核验"
                elif row["subject_scope"] != scope:
                    reason = "FRL复核subject_scope与维度scope不一致"
                elif row["scope_id"] != entity["financing_entity_id"]:
                    reason = "FRL复核融资主体不一致"
                elif row["criterion_id"] not in criterion_ids:
                    reason = "FRL复核指向未登记/非FRL准则"
                if reason:
                    review = {key: row.get(key) for key in review_fields}
                    binding = {"review": review, "qualification_view": None}
                else:
                    review, binding, bind_reason = _bind_dimension_review(
                        case, blobs, row, basis, basis_proofs, review_fields)
                    reason = bind_reason
                if reason:
                    rejected.append({
                        "review_id": row["review_id"], "reason": reason,
                        "evidence_binding": binding,
                    })
                else:
                    reviews.append(review)
                    evidence_bindings.append(binding)
            dimension = evaluate_frl_dimension(
                criteria, reviews, scope=scope, financing_entity=entity,
                applicability=applicability)
            if rejected:
                for item in dimension["criteria"]:
                    item["native_disposition"] = None
                    item["product_status"] = "execution_failed"
                dimension.update(
                    product_status="execution_failed", attained_level=None,
                    first_unmet_level=None, execution_errors=rejected)
            frozen = {
                "dimension_id": "FRL",
                "case_basis": basis,
                "case_basis_version": version,
                "case_basis_proofs": basis_proofs,
                "catalog_sha256": catalog.get("wheel_sha256"),
                "criteria": criteria,
                "reviews": reviews,
                "rejected_reviews": rejected,
                "evidence_bindings": evidence_bindings,
                "scope": scope,
                "scope_id": entity["financing_entity_id"],
                "financing_entity": entity,
                "applicability": applicability,
                "rule_version": dimension["rule_version"],
            }
            digest = sha256_hex(json.dumps(
                frozen, ensure_ascii=False, sort_keys=True).encode("utf-8"))
            result_id_value = f"DIMR2::FRL::{digest}"
            result = {
                "schema_version": "kth-hybrid.dimension-result.v1",
                "result_id": result_id_value,
                "input_digest": digest,
                "dimension": dimension,
                "frozen_inputs": frozen,
                "traceability": {
                    "review_refs": [row["review_id"] for row in reviews],
                    "claim_refs": sorted({row["claim_id"] for row in reviews}),
                    "assessment_unit_refs": entity["assessment_unit_refs"],
                },
            }
            from .audit import validate_dimension_payload
            publish_errors = validate_dimension_payload(case, blobs, result)
            if publish_errors and dimension["product_status"] != "execution_failed":
                for item in dimension["criteria"]:
                    item["native_disposition"] = None
                    item["product_status"] = "execution_failed"
                dimension.update(
                    product_status="execution_failed", attained_level=None,
                    first_unmet_level=None,
                    execution_errors=[{
                        "review_id": "__publish_validation__",
                        "reason": "发布前完整维度绑定核验失败",
                        "broken": publish_errors,
                    }],
                )
                frozen["publish_validation_errors"] = publish_errors
                digest = sha256_hex(json.dumps(
                    frozen, ensure_ascii=False, sort_keys=True).encode("utf-8"))
                result_id_value = f"DIMR2::FRL::{digest}"
                result.update(result_id=result_id_value, input_digest=digest)
            result_bytes = json.dumps(
                result, ensure_ascii=False, sort_keys=True).encode("utf-8")
            result_ref = blobs.put_bytes(result_bytes)
            existing = case.get_dimension_result(digest)
            if existing is None:
                case.add_dimension_result_in_transaction(
                    result_id_value, dimension_id="FRL", input_digest=digest,
                    case_basis_version=version, scope=scope,
                    scope_id=entity["financing_entity_id"],
                    product_status=dimension["product_status"],
                    result_blob_sha256=result_ref.sha256)
        destination = (Path(output_path) if output_path else
                       case_dir / "audit" / "frl-dimension-r2b.json")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        case.new_run(input_digest=digest)
        return {**result, "output_path": str(destination)}
    finally:
        case.close()


def _resolve_assessment_unit(case: CaseStore, blobs: BlobStore,
                             value: dict, scope: str) -> dict:
    required = {
        "scope_id", "subject_scope", "unit_kind", "unit_label",
        "scope_id_ref", "subject_ref", "unit_kind_ref", "unit_label_ref",
    }
    if not isinstance(value, dict) or not required <= set(value):
        raise ValueError("评估单元输入结构不完整")
    if value["subject_scope"] != scope:
        raise ValueError("评估单元主体与Case主体不一致")
    proof_bindings = _resolve_reference_bundle(case, blobs, [
        ("scope_id", value["scope_id"], value["scope_id_ref"]),
        ("subject", scope, value["subject_ref"]),
        ("unit_kind", value["unit_kind"], value["unit_kind_ref"]),
        ("unit_label", value["unit_label"], value["unit_label_ref"]),
    ])
    return {
        "scope_id": value["scope_id"],
        "subject_scope": scope,
        "unit_kind": value["unit_kind"],
        "unit_label": value["unit_label"],
        "proof_bindings": proof_bindings,
    }


def _run_assessment_unit_dimension_slice(
        case_dir: Path | str, *, catalog: dict, case_basis: dict, scope: str,
        assessment_unit: dict, dimension_id: str, evaluator,
        output_name: str) -> dict:
    case_dir = Path(case_dir)
    blobs = BlobStore(case_dir / "blobs")
    case = CaseStore(case_dir / "records.sqlite3")
    try:
        basis, version = _effective_case_basis(case, case_basis)
        if scope != basis["subject_legal_name"]:
            raise ValueError(f"{dimension_id}维度scope必须等于冻结Case主体")
        if not isinstance(assessment_unit, dict) \
                or assessment_unit.get("subject_scope") != scope:
            raise ValueError("评估单元主体与Case主体不一致")
        raw_scope_id = assessment_unit.get("scope_id")
        if not isinstance(raw_scope_id, str) or not raw_scope_id.strip():
            raise ValueError("评估单元scope_id不能为空")
        with case.immediate_transaction():
            reviews, rejected, evidence_bindings = [], [], []
            basis_proofs, basis_error = resolve_case_basis_proof_bindings(
                basis, case, blobs)
            if basis_error or basis_proofs is None:
                rejected.append({
                    "review_id": "__case_basis__",
                    "reason": f"CaseBasis证明不可核验：{basis_error or '无绑定'}",
                })
                basis_proofs = None
            try:
                unit = _resolve_assessment_unit(
                    case, blobs, assessment_unit, scope)
                unit_error = None
            except ValueError as exc:
                unit_error = str(exc)
                unit = {
                    "scope_id": raw_scope_id,
                    "subject_scope": scope,
                    "unit_kind": assessment_unit.get("unit_kind") or "__invalid__",
                    "unit_label": assessment_unit.get("unit_label") or "__invalid__",
                    "proof_bindings": {},
                }
                rejected.append({
                    "review_id": "__assessment_unit__",
                    "reason": f"评估单元证明不可核验：{unit_error}",
                })
            criteria = [dict(row) for row in catalog["dimensions"][
                dimension_id]["registry"]["criteria"]]
            criterion_ids = {row["criterion_id"] for row in criteria}
            review_fields = (
                "review_id", "dimension_id", "criterion_id", "claim_id",
                "quote_sha256", "decision", "evidence_class", "findings",
                "subject_scope", "scope_id", "support_scope", "reviewer",
                "review_basis",
            )
            rows = case.fetch_dimension_evidence_reviews(
                dimension_id, version, raw_scope_id)
            for row in rows:
                reason = None
                if basis_proofs is None:
                    reason = "CaseBasis证明不可核验"
                elif unit_error:
                    reason = "评估单元证明不可核验"
                elif row["subject_scope"] != scope:
                    reason = f"{dimension_id}复核subject_scope与维度scope不一致"
                elif row["criterion_id"] not in criterion_ids:
                    reason = f"{dimension_id}复核指向未登记准则"
                if reason:
                    review = {key: row.get(key) for key in review_fields}
                    binding = {"review": review, "qualification_view": None}
                else:
                    review, binding, reason = _bind_dimension_review(
                        case, blobs, row, basis, basis_proofs, review_fields)
                if reason:
                    rejected.append({
                        "review_id": row["review_id"], "reason": reason,
                        "evidence_binding": binding,
                    })
                else:
                    reviews.append(review)
                    evidence_bindings.append(binding)
            dimension = evaluator(
                criteria, reviews, scope=scope, assessment_unit=unit)
            if rejected:
                for item in dimension["criteria"]:
                    item["native_disposition"] = None
                    item["product_status"] = "execution_failed"
                dimension.update(
                    product_status="execution_failed", attained_level=None,
                    first_unmet_level=None, execution_errors=rejected)
            frozen = {
                "dimension_id": dimension_id,
                "case_basis": basis,
                "case_basis_version": version,
                "case_basis_proofs": basis_proofs,
                "catalog_sha256": catalog.get("wheel_sha256"),
                "criteria": criteria,
                "reviews": reviews,
                "rejected_reviews": rejected,
                "evidence_bindings": evidence_bindings,
                "scope": scope,
                "scope_id": raw_scope_id,
                "assessment_scope": unit,
                "rule_version": dimension["rule_version"],
            }
            digest = sha256_hex(json.dumps(
                frozen, ensure_ascii=False, sort_keys=True).encode("utf-8"))
            result_id_value = f"DIMR2::{dimension_id}::{digest}"
            result = {
                "schema_version": "kth-hybrid.dimension-result.v1",
                "result_id": result_id_value,
                "input_digest": digest,
                "dimension": dimension,
                "frozen_inputs": frozen,
                "traceability": {
                    "review_refs": [row["review_id"] for row in reviews],
                    "claim_refs": sorted({row["claim_id"] for row in reviews}),
                    "assessment_unit_ref": raw_scope_id,
                },
            }
            from .audit import validate_dimension_payload
            publish_errors = validate_dimension_payload(case, blobs, result)
            if publish_errors and dimension["product_status"] != "execution_failed":
                for item in dimension["criteria"]:
                    item["native_disposition"] = None
                    item["product_status"] = "execution_failed"
                dimension.update(
                    product_status="execution_failed", attained_level=None,
                    first_unmet_level=None,
                    execution_errors=[{
                        "review_id": "__publish_validation__",
                        "reason": "发布前完整维度绑定核验失败",
                        "broken": publish_errors,
                    }],
                )
                frozen["publish_validation_errors"] = publish_errors
                digest = sha256_hex(json.dumps(
                    frozen, ensure_ascii=False, sort_keys=True).encode("utf-8"))
                result_id_value = f"DIMR2::{dimension_id}::{digest}"
                result.update(result_id=result_id_value, input_digest=digest)
            result_ref = blobs.put_bytes(json.dumps(
                result, ensure_ascii=False, sort_keys=True).encode("utf-8"))
            if case.get_dimension_result(digest) is None:
                case.add_dimension_result_in_transaction(
                    result_id_value, dimension_id=dimension_id,
                    input_digest=digest, case_basis_version=version,
                    scope=scope, scope_id=raw_scope_id,
                    product_status=dimension["product_status"],
                    result_blob_sha256=result_ref.sha256)
        destination = case_dir / "audit" / output_name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        case.new_run(input_digest=digest)
        return {**result, "output_path": str(destination)}
    finally:
        case.close()


def run_brl_dimension_slice(case_dir: Path | str, *, catalog: dict,
                            case_basis: dict, scope: str,
                            assessment_unit: dict) -> dict:
    """夜间BRL候选Case入口。"""
    return _run_assessment_unit_dimension_slice(
        case_dir, catalog=catalog, case_basis=case_basis, scope=scope,
        assessment_unit=assessment_unit, dimension_id="BRL",
        evaluator=evaluate_brl_dimension, output_name="brl-dimension-night.json")


class CountingSimulatedProvider:
    """计数型本地模拟 Provider（simulated=true，非真实模型角色链）。"""

    simulated = True

    def __init__(self, journal: Journal, blobs: BlobStore | None = None,
                 worker_id: str = "simulated-provider"):
        if blobs is None:
            raise ValueError("v2 要求提供 BlobStore：响应必须先持久化再提交")
        self.journal = journal
        self.blobs = blobs
        self.worker_id = worker_id
        self.dispatch_count = 0

    def execute(self, task_key: str, input_id: str,
                response_factory: Callable[[], bytes], *,
                crash_after_dispatch: bool = False,
                crash_after_persist_before_commit: bool = False) -> str:
        claim = self.journal.claim(task_key, self.worker_id, input_id)
        self.journal.record_dispatch(claim)
        self.dispatch_count += 1
        if crash_after_dispatch:
            raise SimulatedCrash(f"任务 {task_key} 于派发后、响应持久前崩溃")
        response = response_factory()
        persisted = self.blobs.put_bytes(response)
        if self.blobs.read_bytes(persisted) != response:
            raise RuntimeError(f"任务 {task_key} 响应持久化复核失败")
        output_ref = persisted.sha256
        if crash_after_persist_before_commit:
            raise SimulatedCrash(
                f"任务 {task_key} 于响应持久化后、DB提交前崩溃（孤立响应工件保留）"
            )
        self.journal.commit(claim, output_ref)
        return output_ref


def _same_body_occurrence_count(case: CaseStore, blob_sha256: str) -> int:
    return sum(1 for row in case.fetch_all("sources")
               if row["blob_sha256"] == blob_sha256)


def _projection_excerpt(blobs: BlobStore, source: dict, locator_ref: dict):
    data = blobs.read_bytes(source["blob_sha256"])
    if "page" in locator_ref:
        projection = extract_pdf_pages(data)
        for row in projection.locators:
            if row["page"] == locator_ref["page"]:
                return row
        raise ValueError(f"第 {locator_ref['page']} 页无可用文本投影")
    if "member" in locator_ref:
        import io
        import zipfile

        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            member_data = archive.read(locator_ref["member"])
        projection = extract_docx_paragraphs(member_data)
        for row in projection.locators:
            if row["paragraph"] == locator_ref.get("paragraph"):
                return row
        raise ValueError(
            f"成员 {locator_ref['member']} 第 {locator_ref.get('paragraph')} 段无投影")
    raise ValueError(f"未知 locator_ref：{locator_ref}")


def _sealed_excerpt_bytes(blobs: BlobStore, source: dict, claim_spec: dict):
    """返回（封存摘录字节, 摘录hash, 摘录文本, 定位字段）。"""
    locator_kind = claim_spec["locator_kind"]
    if locator_kind == "byte_range":
        data = blobs.read_bytes(source["blob_sha256"])
        start, end = claim_spec["start"], claim_spec["end"]
        if not (0 <= start < end <= len(data)):
            raise ValueError(f"主张区间非法 [{start},{end})（对象长度 {len(data)}）")
        excerpt = data[start:end]
        return (excerpt, sha256_hex(excerpt),
                claim_spec.get("excerpt_text")
                or excerpt.decode("utf-8", errors="replace"),
                start, end, None)
    row = _projection_excerpt(blobs, source, claim_spec["locator_ref"])
    return (row["text"].encode("utf-8"), row["text_sha256"], row["text"],
            None, None,
            json.dumps(claim_spec["locator_ref"], ensure_ascii=False))


def _verify_candidate(case: CaseStore, blobs: BlobStore,
                      candidate_row: dict):
    """发布前验证（R1.3-C）：对候选结果执行与 trace 相同的绑定核验。

    返回 audit.TraceReport（ok=True 方可发布成功）。验证中断（异常）时
    调用方不得执行 INSERT——不发布未核验的成功。
    """
    from .audit import verify_result_bindings

    return verify_result_bindings(case, blobs, candidate_row)


_CASE_BASIS_FIELDS = (
    "subject_legal_name", "subject_aliases", "evidence_cutoff",
    "subject_source_basis",
)


def _requested_case_basis(case_basis: dict) -> dict:
    """只接受运行会实际消费的 CaseBasis 字段，忽略调用方附带声明。"""
    missing = [field for field in _CASE_BASIS_FIELDS if field not in case_basis]
    if missing:
        raise ValueError(f"case_basis 缺少必需字段：{missing}")
    aliases = case_basis["subject_aliases"]
    if not isinstance(aliases, list) or not all(isinstance(item, str) for item in aliases):
        raise ValueError("case_basis.subject_aliases 必须为字符串列表")
    return {
        "subject_legal_name": case_basis["subject_legal_name"],
        "subject_aliases": aliases,
        "evidence_cutoff": case_basis["evidence_cutoff"],
        "subject_source_basis": case_basis["subject_source_basis"],
    }


def _effective_case_basis(case: CaseStore, supplied_basis: dict) -> tuple[dict, int]:
    """建立或读取唯一有效依据；已存在 Case 不允许同结果改用调用方 basis。"""
    requested = _requested_case_basis(supplied_basis)
    current = case.get_case_basis()
    if current is None:
        version = case.set_case_basis(**requested)
    else:
        version = current["version"]
        stored = case.get_case_basis_version(version)
        if stored is None:
            raise RuntimeError(f"CaseBasis 版本 {version} 不存在，不能继续求值")
        changed = [field for field in _CASE_BASIS_FIELDS
                   if stored.get(field) != requested.get(field)]
        if changed:
            raise RuntimeError(
                "有效 CaseBasis 与调用方输入不一致（差异字段："
                f"{changed}）；同一Case/结果不得改用新主体、别名或截止重放"
            )
    snapshot = case.get_case_basis_version(version)
    if snapshot is None:
        raise RuntimeError(f"CaseBasis 版本 {version} 快照缺失，不能继续求值")
    return ({key: value for key, value in snapshot.items() if key != "version"},
            version)


def _subject_provenance_binding(case_basis: dict, case: CaseStore,
                                blobs: BlobStore) -> dict:
    """解析主体引用并冻结实际读取的 provenance 对象、字段和值。"""
    try:
        reference = json.loads(case_basis["subject_source_basis"])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"subject_source_basis 不是合法 JSON 引用：{exc}") from exc
    _value, error, binding = resolve_case_field_reference_binding(
        reference, case, blobs, expect_value=case_basis["subject_legal_name"])
    if error or binding is None:
        raise ValueError(f"主体来源依据不可核验：{error or '未生成封存对象绑定'}")
    return binding


def run_criterion_slice(case_dir: Path | str, *, source_id: str, claim_spec: dict,
                        criterion_id: str, catalog: dict,
                        case_basis: dict | None = None,
                        case_flags: dict | None = None,
                        review_attempt: str = "r1_3-runner") -> dict:
    """执行一条判据切片并落库（v4：预解析＋确认映射＋完整冻结＋原子发布）。"""
    case_dir = Path(case_dir)
    if not case_basis or not case_basis.get("subject_source_basis"):
        raise ValueError("case_basis 必须携带 subject_source_basis（有源主体/截止）")
    canonical = resolve_criterion(criterion_id, catalog)
    approved = approved_ids_from_catalog(catalog)
    blobs = BlobStore(case_dir / "blobs")
    case = CaseStore(case_dir / "records.sqlite3")
    try:
        # 资格、冻结、摘要和 trace 共同消费这一份存储版本快照。调用方仅能
        # 在Case尚无依据时建立首个版本；后续同ID重放不得偷换截止或别名。
        basis, basis_version = _effective_case_basis(case, case_basis)
        basis_proofs, basis_proof_error = resolve_case_basis_proof_bindings(
            basis, case, blobs)
        if basis_proof_error or basis_proofs is None:
            raise ValueError(
                f"CaseBasis 证明不可核验：{basis_proof_error or '无证明绑定'}")
        subject_proof = basis_proofs["subject"]
        source = case.fetch_one("sources", "source_id", source_id)
        if source is None:
            raise KeyError(f"来源 {source_id} 不存在（先运行 census 导入）")
        # 所用时间证据=最新修订；记录修订号与内容快照（冻结不可变版本）
        latest_time = case.latest_time_evidence(source_id)
        time_revision = latest_time["revision"] if latest_time else None
        time_snapshot = ({k: v for k, v in latest_time.items()
                          if k not in ("revision", "created_at")}
                         if latest_time else None)
        source = dict(source)
        if time_snapshot is not None:
            source["time_evidence"] = time_snapshot
        elif source.get("time_evidence"):
            try:
                source["time_evidence"] = json.loads(source["time_evidence"])
            except (TypeError, ValueError):
                source["time_evidence"] = {}

        proof_bindings = {"case_basis": basis_proofs}
        document_proof, document_proof_error = resolve_document_subject_proof_bindings(
            source, basis["subject_legal_name"], case, blobs,
            effective_subject_names=set(
                basis_proofs.get("effective_subject_names", [
                    basis["subject_legal_name"]])),
        )
        if document_proof is not None:
            proof_bindings["document_subject"] = document_proof
        elif source.get("document_subject") and document_proof_error:
            proof_bindings["document_subject_error"] = document_proof_error
        if isinstance(source.get("time_evidence"), dict) \
                and source["time_evidence"].get("timezone_rule"):
            _tz, timezone_proof, timezone_error = resolve_timezone_rule_binding(
                source["time_evidence"], source, case, blobs)
            if timezone_proof is not None:
                proof_bindings["timezone"] = timezone_proof
            else:
                proof_bindings["timezone_error"] = timezone_error

        excerpt, excerpt_sha256, excerpt_text, start, end, locator_ref = \
            _sealed_excerpt_bytes(blobs, source, claim_spec)
        if claim_spec.get("excerpt_text") and \
                claim_spec["locator_kind"] == "byte_range" and \
                claim_spec["excerpt_text"].encode("utf-8") != excerpt:
            raise ValueError("excerpt_text 与封存区间不一致（不接受自报摘录文本）")

        claim_id = claim_spec["claim_id"]
        claim_row = {
            "claim_id": claim_id, "source_id": source_id,
            "locator_kind": claim_spec["locator_kind"],
            "locator_start": start or 0, "locator_end": end or 0,
            "locator_ref": locator_ref,
            "excerpt_sha256": excerpt_sha256, "excerpt_text": excerpt_text,
            "interpretation": claim_spec["interpretation"],
            "subject_scope": claim_spec["subject_scope"],
        }
        content_digest = claim_content_digest(claim_row)
        existing = case.fetch_one("claims", "claim_id", claim_id)
        if existing is not None:
            stored_content = claim_content_digest(existing)
            if stored_content != content_digest:
                raise RuntimeError(
                    f"输入身份不一致：claim {claim_id} 已以不同输入登记"
                    f"（存档内容摘要 {stored_content[:12]}…，本次 "
                    f"{content_digest[:12]}…）。同ID不同解释/主体/来源/定位必须"
                    f"新建claim版本，不得覆盖"
                )
        else:
            case.add_claim(
                claim_id, source_id, locator_kind=claim_row["locator_kind"],
                excerpt_start=claim_row["locator_start"],
                excerpt_end=claim_row["locator_end"],
                excerpt_sha256=excerpt_sha256, excerpt_text=excerpt_text,
                interpretation=claim_row["interpretation"],
                subject_scope=claim_row["subject_scope"],
                interpretation_attempt=review_attempt, locator_ref=locator_ref,
                input_digest=content_digest, content_digest=content_digest,
            )
        claim = case.fetch_one("claims", "claim_id", claim_id)

        # 映射：关键词候选 → 留痕确认（否定门控）
        mapping_spec = claim_spec.get("criterion_mapping")
        if mapping_spec is not None:
            candidate_mapping = interpret_claim_for_criterion(
                mapping_spec.get("quote") or "", criterion_id, excerpt,
                int(mapping_spec.get("start", -1)),
                int(mapping_spec.get("end", -1)))
        else:
            candidate_mapping = {"status": "unmapped", "filter_hits": [],
                                 "quote_sha256": None,
                                 "basis": "调用方未提供判据映射引文"}
        mapping_review = _controlled_mapping_review(
            case, claim_spec.get("mapping_review_id"), basis_version=basis_version,
            claim_id=claim_id, criterion_id=criterion_id, mapping=candidate_mapping)
        mapping = confirm_mapping(
            candidate_mapping, mapping_review,
            interpretation=claim_spec.get("interpretation") or "")
        case.add_claim_mapping(
            claim_id, criterion_id,
            quote_start=int(mapping_spec.get("start", 0)) if mapping_spec else 0,
            quote_end=int(mapping_spec.get("end", 0)) if mapping_spec else 0,
            quote_sha256=mapping.get("quote_sha256") or "",
            filter_hits=mapping.get("filter_hits") or [],
            status=mapping.get("status", "unmapped"),
        )

        outcome = qualify_claim(
            claim, source, blobs, basis,
            same_body_sources=_same_body_occurrence_count(case, source["blob_sha256"]),
            review_attempt=review_attempt, case=case,
        )
        qual_refs: list[str] = []
        gap_refs: list[str] = []
        qual_digest = ""
        if isinstance(outcome, GapOutcome):
            gap_id = f"GAPR::{outcome.claim_id}"
            if case.fetch_one("gaps", "gap_id", gap_id) is None:
                case.add_gap(
                    gap_id, outcome.gap_type,
                    affected_criteria=[criterion_id],
                    pipeline_fault=outcome.pipeline_fault,
                    investigation=outcome.investigation,
                    unconfirmed=outcome.unconfirmed,
                )
            gap_refs.append(gap_id)
        else:
            qual_id = qualification_id(claim_id)
            if case.fetch_one("qualifications", "qual_id", qual_id) is None:
                case.add_qualification(
                    qual_id, outcome.claim_id,
                    source_judgment=outcome.source_judgment.basis,
                    identity_judgment=outcome.identity_judgment.basis,
                    time_judgment=outcome.time_judgment.basis,
                    independence_judgment=outcome.independence_judgment.basis,
                    allowed_uses=outcome.allowed_uses,
                    cannot_prove=outcome.cannot_prove,
                    review_attempt=outcome.review_attempt,
                    status=outcome.status,
                )
            qual_row = case.fetch_one("qualifications", "qual_id", qual_id)
            qual_digest = qualification_content_digest(qual_row)
            qual_refs.append(qual_id)

        # N/A 预解析（封存对象字段值）
        na_proposal = _resolve_na_proposal(
            claim_spec.get("na_proposal"), case, blobs)

        frozen_inputs = {
            "criterion": {k: canonical.get(k) for k in
                          ("criterion_id", "dimension", "level", "text",
                           "na_policy")},
            "catalog_sha256": catalog.get("wheel_sha256", ""),
            "approved_ids": sorted(approved),
            "rule_version": RULE_VERSION,
            "qualification_version": QUALIFICATION_VERSION,
            "case_basis": basis,
            "case_basis_version": basis_version,
            "case_provenance_bindings": basis_proofs,
            "proof_bindings": proof_bindings,
            "case_flags": case_flags or {},
            "case_subject": basis["subject_legal_name"],
            "mapping": {
                "status": mapping.get("status"),
                "quote_sha256": mapping.get("quote_sha256"),
                "quote_start": (int(mapping_spec.get("start", 0))
                                if mapping_spec else 0),
                "quote_end": (int(mapping_spec.get("end", 0))
                              if mapping_spec else 0),
                "confirmation": mapping.get("confirmation"),
            },
            "source_inputs": {
                "published_at": source.get("published_at"),
                "retrieved_at": source.get("retrieved_at"),
                "source_family": source.get("source_family"),
                "capture_status": source.get("capture_status"),
                "document_subject": source.get("document_subject"),
                "time_evidence_revision": time_revision,
                "time_evidence_snapshot": time_snapshot,
            },
            "qualification_digest": qual_digest,
            "na_proposal": na_proposal,
        }
        digest = run_input_digest_v3(frozen_inputs, claim)

        evidence_view = {
            "case_flags": case_flags or {},
            "case_subject": basis["subject_legal_name"],
            "dimension_levels_supported": canonical.get("levels_supported")
            or catalog["dimensions"][canonical["dimension"]]["levels_supported"],
            "scope": claim_spec["subject_scope"],
            "approved_criterion_ids": approved,
            "catalog_criterion": canonical,
        }
        judgment_candidate = {
            "qualifications": [
                asdict(outcome) if isinstance(outcome, QualificationOutcome)
                else {"claim_id": outcome.claim_id, "status": "rejected",
                      "allowed_uses": [], "identity_judgment": {"verdict": "unknown"}}
            ],
            "claims": {claim_id: claim},
            "gap_refs": gap_refs,
            "na_proposal": na_proposal,
            "native_proposal": claim_spec.get("native_proposal"),
            "criterion_mapping": mapping,
        }
        evaluation: CriterionEvaluation = evaluate_criterion(
            canonical, judgment_candidate, evidence_view
        )

        rid = result_id(claim_id, criterion_id)
        existing_result = case.fetch_one("criterion_results", "result_id", rid)
        if existing_result is not None:
            if existing_result["input_digest"] != digest:
                raise RuntimeError(
                    f"重复计算输入摘要不一致：{rid} 存档 "
                    f"{(existing_result['input_digest'] or '无')[:12]}… vs 本次 "
                    f"{digest[:12]}…（冻结输入被改动或换依据重放：时间/N-A依据/"
                    "映射确认/来源字段任一变化即不同输入）"
                )
            if existing_result["product_status"] != evaluation.product_status:
                raise RuntimeError(
                    f"重复计算业务字段不一致：{rid} 已存 "
                    f"{existing_result['product_status']} vs 本次 "
                    f"{evaluation.product_status}"
                )
            published_status = existing_result["product_status"]
        else:
            published_status = evaluation.product_status

        na_basis = None
        if evaluation.product_status == "succeeded" and na_proposal:
            na_basis = {
                "basis": str(((na_proposal.get("applicability_resolved")
                               or {}).get("value")) or ""),
                "case_flag_source": (na_proposal.get("flag_resolved") or {}
                                     ).get("path"),
            }

        candidate_row = {
            "result_id": rid, "criterion_id": criterion_id,
            "dimension": canonical["dimension"],
            "native_disposition": evaluation.native_disposition,
            "native_note": evaluation.native_note,
            "product_status": published_status,
            "evidence_refs": json.dumps(evaluation.evidence_refs,
                                        ensure_ascii=False),
            "gap_refs": json.dumps(evaluation.gap_refs, ensure_ascii=False),
            "qual_refs": json.dumps(qual_refs, ensure_ascii=False),
            "rationale": evaluation.rationale,
            "scope": evaluation.scope, "rule_version": evaluation.rule_version,
            "input_digest": digest, "case_basis_version": basis_version,
            "frozen_inputs": json.dumps(frozen_inputs, ensure_ascii=False,
                                        sort_keys=True),
            "na_basis": json.dumps(na_basis, ensure_ascii=False) if na_basis
            else None,
        }
        # 验证读取与结果写入被同一 BEGIN IMMEDIATE 保护。先前发生的变更会
        # 被完整核验发现；事务期间另一连接不能插入破坏验证结论的改写。
        if existing_result is None:
            with case.immediate_transaction():
                report = _verify_candidate(case, blobs, candidate_row)
                if report.ok:
                    case.add_criterion_result_in_transaction(
                        rid, criterion_id, canonical["dimension"],
                        native_disposition=evaluation.native_disposition,
                        native_note=evaluation.native_note,
                        product_status=evaluation.product_status,
                        evidence_refs=evaluation.evidence_refs,
                        gap_refs=evaluation.gap_refs, qual_refs=qual_refs,
                        rationale=evaluation.rationale, scope=evaluation.scope,
                        rule_version=evaluation.rule_version, input_digest=digest,
                        case_basis_version=basis_version,
                        frozen_inputs=frozen_inputs, na_basis=na_basis)
                    published_status = evaluation.product_status
                else:
                    failure_rationale = (
                        "发布前验证失败，保留为失败候选（非业务NO；验证未通过不得"
                        "发布成功）：" + "；".join(report.broken[:3]))
                    case.add_criterion_result_in_transaction(
                        rid, criterion_id, canonical["dimension"],
                        native_disposition=evaluation.native_disposition,
                        native_note=evaluation.native_note,
                        product_status="execution_failed",
                        evidence_refs=evaluation.evidence_refs,
                        gap_refs=evaluation.gap_refs, qual_refs=qual_refs,
                        rationale=failure_rationale, scope=evaluation.scope,
                        rule_version=evaluation.rule_version, input_digest=digest,
                        case_basis_version=basis_version,
                        frozen_inputs=frozen_inputs, na_basis=na_basis)
                    published_status = "execution_failed"
        else:
            report = _verify_candidate(case, blobs, candidate_row)

        case.new_run(input_digest=digest)
        replay_consistent = True
        previous_qual = case.fetch_one("qualifications", "qual_id",
                                       qualification_id(claim_id))
        if previous_qual is not None and not isinstance(outcome, GapOutcome):
            replay_consistent = previous_qual["status"] == outcome.status
        return {
            "result_id": rid,
            "claim_id": claim_id,
            "input_digest": digest,
            "content_digest": content_digest,
            "qualification_status": (
                outcome.status if isinstance(outcome, QualificationOutcome)
                else f"gap:{outcome.gap_type}"
            ),
            "mapping_status": mapping.get("status"),
            "product_status": published_status,
            "native_disposition": evaluation.native_disposition,
            "rationale": evaluation.rationale,
            "trace_ok": report.ok,
            "replay_consistent": replay_consistent,
        }
    finally:
        case.close()
