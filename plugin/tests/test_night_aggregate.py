"""夜间六维离线候选汇总。"""
import os
from pathlib import Path
import pytest
from kth_hybrid.aggregate import validate_dimension_index,build_offline_dimension_view
from kth_hybrid.store import CaseStore,BlobStore
EXPECTED={"CRL","BRL","TRL","IPRL","TMRL","FRL"}
def rows():return [{"dimension_id":d,"scope":"Company-A","case_basis_version":1,"result_id":d+"-R","input_digest":d+"-D","product_status":"insufficient","attained_level":0 if d!="CRL" else None,"trace_ok":True} for d in EXPECTED]
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
 root=Path(REAL);case=CaseStore(root/'records.sqlite3')
 try:view=build_offline_dimension_view(case,BlobStore(root/'blobs'))
 finally:case.close()
 assert set(view["dimensions"])==EXPECTED
 assert view["status"]=="offline_candidate"
 assert "total_score" not in view and "decision" not in view
 assert all(x["trace_ok"] for x in view["dimensions"].values())
