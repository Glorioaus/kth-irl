"""显式manifest驱动的六维离线候选汇总。"""
import os
from pathlib import Path
import pytest
from kth_hybrid.aggregate import (freeze_aggregation_manifest,
                                  validate_dimension_index,
                                  build_offline_dimension_view)
from kth_hybrid.catalog import APPROVED_WHEEL_SHA256
from kth_hybrid.catalog import build_catalog_from_wheel
from kth_hybrid import runner
from kth_hybrid.store import CaseStore,BlobStore
EXPECTED={"CRL","BRL","TRL","IPRL","TMRL","FRL"}
def rows():
 out=[]
 for d in EXPECTED:
  row={"dimension_id":d,"scope":"Company-A","case_basis_version":1,
       "result_id":d+"-R","input_digest":d+"-D","product_status":"insufficient",
       "attained_level":0 if d!="CRL" else None,"trace_ok":True,
       "catalog_sha256":APPROVED_WHEEL_SHA256,"rule_version":"method-v1",
       "result_schema_version":"schema-v1","scope_id":None,
       "assessment_scope":None,"financing_entity":None}
  if d in {"BRL","TRL","IPRL","TMRL"}:
   row.update(scope_id="UNIT-A",assessment_scope={"scope_id":"UNIT-A","subject_scope":"Company-A"})
  if d=="FRL":
   row.update(scope_id="FIN-A",financing_entity={"financing_entity_id":"FIN-A","subject_scope":"Company-A","assessment_unit_refs":["UNIT-A"]})
  out.append(row)
 return out
def test_index_requires_exact_six_dimensions_common_scope_and_trace():
 assert set(validate_dimension_index(rows(),scope="Company-A",case_basis_version=1))==EXPECTED
 with pytest.raises(ValueError,match="缺失"):validate_dimension_index(rows()[:-1],scope="Company-A",case_basis_version=1)
 bad=rows();bad[0]["scope"]="Other"
 with pytest.raises(ValueError,match="scope"):validate_dimension_index(bad,scope="Company-A",case_basis_version=1)
 bad=rows();bad[0]["trace_ok"]=False
 with pytest.raises(ValueError,match="trace"):validate_dimension_index(bad,scope="Company-A",case_basis_version=1)
REAL=os.environ.get("KTH_REAL_CASE_DIR_NIGHT")
@pytest.mark.skipif(not REAL,reason="未设置KTH_REAL_CASE_DIR_NIGHT")
def test_real_offline_six_dimension_view_is_traceable_not_a_decision():
 root=Path(REAL);case=CaseStore(root/'records.sqlite3');basis=case.get_case_basis();case.close();scope=basis["subject_legal_name"];catalog=build_catalog_from_wheel()
 unit={"scope_id":"UNIT-WEIJU-COMPANY-CURRENT-CASE","subject_scope":scope,"unit_kind":"current_case_company_level","unit_label":"微玖当前Case公司级候选单元","scope_id_ref":{"kind":"field_reference","path":"case:assessment-units.json#/units/0/scope_id"},"subject_ref":{"kind":"field_reference","path":"case:assessment-units.json#/units/0/subject"},"unit_kind_ref":{"kind":"field_reference","path":"case:assessment-units.json#/units/0/kind"},"unit_label_ref":{"kind":"field_reference","path":"case:assessment-units.json#/units/0/label"}}
 entity={"financing_entity_id":"FIN-WEIJU-COMPANY","subject_scope":scope,"assessment_unit_refs":["UNIT-WEIJU-COMPANY-CURRENT-CASE"],"entity_ref":{"kind":"field_reference","path":"case:frl-scope.json#/entity_id"},"subject_ref":{"kind":"field_reference","path":"case:frl-scope.json#/subject"},"assessment_units_ref":{"kind":"field_reference","path":"case:frl-scope.json#/units"}}
 results={"CRL":runner.run_crl_dimension_slice(root,catalog=catalog,case_basis=basis,scope=scope),"FRL":runner.run_frl_dimension_slice(root,catalog=catalog,case_basis=basis,scope=scope,financing_entity=entity)}
 for d,fn in (("BRL",runner.run_brl_dimension_slice),("TRL",runner.run_trl_dimension_slice),("IPRL",runner.run_iprl_dimension_slice),("TMRL",runner.run_tmrl_dimension_slice)):
  results[d]=fn(root,catalog=catalog,case_basis=basis,scope=scope,assessment_unit=unit)
 case=CaseStore(root/'records.sqlite3');blobs=BlobStore(root/'blobs')
 try:
  ids={d:result["result_id"] for d,result in results.items()}
  manifest=freeze_aggregation_manifest(case,blobs,ids)
  view=build_offline_dimension_view(case,blobs,manifest)
 finally:case.close()
 assert set(view["dimensions"])==EXPECTED and view["status"]=="offline_candidate"
 assert "total_score" not in view and "decision" not in view
 assert all(x["trace_ok"] for x in view["dimensions"].values())
