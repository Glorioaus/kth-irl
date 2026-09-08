"""本夜真实综合门控：六维实际结果、trace、汇总和离线角色合同。"""
import os
from pathlib import Path
import pytest
from kth_hybrid import runner
from kth_hybrid.aggregate import build_offline_dimension_view
from kth_hybrid.audit import trace_crl_dimension,trace_dimension_result
from kth_hybrid.catalog import build_catalog_from_wheel
from kth_hybrid.roles import assemble_offline_deliberation
from kth_hybrid.store import BlobStore,CaseStore
ROOT=os.environ.get("KTH_REAL_CASE_DIR_NIGHT")
def _attempt(view,role,producer,context):
 return {"schema_version":"kth-hybrid.offline-role-attempt.v1","role":role,"producer_id":producer,"context_id":context,"simulated":True,"input_view_id":view["view_id"],"input_digest":view["input_digest"],"scope":view["scope"],"dimension_result_refs":{d:r["result_id"] for d,r in view["dimensions"].items()},"candidate_id":role+"-NIGHT-C1","statement":"基于冻结六维不足状态的离线候选，不形成决定。","evidence_refs":["OFFLINE-SIX-DIMENSION-VIEW"],"limitations":["未调用真实模型"]}
@pytest.mark.skipif(not ROOT,reason="未设置KTH_REAL_CASE_DIR_NIGHT")
def test_real_six_dimension_and_offline_role_gate():
 root=Path(ROOT);case=CaseStore(root/'records.sqlite3');basis=case.get_case_basis();case.close();scope=basis['subject_legal_name'];catalog=build_catalog_from_wheel()
 unit={"scope_id":"UNIT-WEIJU-COMPANY-CURRENT-CASE","subject_scope":scope,"unit_kind":"current_case_company_level","unit_label":"微玖当前Case公司级候选单元","scope_id_ref":{"kind":"field_reference","path":"case:assessment-units.json#/units/0/scope_id"},"subject_ref":{"kind":"field_reference","path":"case:assessment-units.json#/units/0/subject"},"unit_kind_ref":{"kind":"field_reference","path":"case:assessment-units.json#/units/0/kind"},"unit_label_ref":{"kind":"field_reference","path":"case:assessment-units.json#/units/0/label"}}
 entity={"financing_entity_id":"FIN-WEIJU-COMPANY","subject_scope":scope,"assessment_unit_refs":["UNIT-WEIJU-COMPANY-CURRENT-CASE"],"entity_ref":{"kind":"field_reference","path":"case:frl-scope.json#/entity_id"},"subject_ref":{"kind":"field_reference","path":"case:frl-scope.json#/subject"},"assessment_units_ref":{"kind":"field_reference","path":"case:frl-scope.json#/units"}}
 results={"CRL":runner.run_crl_dimension_slice(root,catalog=catalog,case_basis=basis,scope=scope),"FRL":runner.run_frl_dimension_slice(root,catalog=catalog,case_basis=basis,scope=scope,financing_entity=entity)}
 for d,fn in (("BRL",runner.run_brl_dimension_slice),("TRL",runner.run_trl_dimension_slice),("IPRL",runner.run_iprl_dimension_slice),("TMRL",runner.run_tmrl_dimension_slice)):
  results[d]=fn(root,catalog=catalog,case_basis=basis,scope=scope,assessment_unit=unit)
 case=CaseStore(root/'records.sqlite3');blobs=BlobStore(root/'blobs')
 try:
  assert trace_crl_dimension(case,blobs,results["CRL"]["result_id"])["ok"]
  assert all(trace_dimension_result(case,blobs,results[d]["result_id"])["ok"] for d in ("FRL","BRL","TRL","IPRL","TMRL"))
  view=build_offline_dimension_view(case,blobs)
 finally:case.close()
 pro=_attempt(view,"PRO","P","CTX-P");con=_attempt(view,"CON","C","CTX-C");chair=_attempt(view,"CHAIR","H","CTX-H")
 round1={"round_number":1,"pro_response":{"producer_id":"P","observed_candidate_id":con["candidate_id"],"statement":"回应不足边界"},"con_response":{"producer_id":"C","observed_candidate_id":pro["candidate_id"],"statement":"回应不足边界"}}
 deliberation=assemble_offline_deliberation(view,pro,con,[round1],chair,owner_selected_round_count=1)
 assert deliberation["status"]=="simulated_offline_candidate" and "decision" not in deliberation
