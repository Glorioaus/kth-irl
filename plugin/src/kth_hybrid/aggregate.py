"""六维离线候选索引；不计算总分或投资决定。"""
import json
from .audit import trace_crl_dimension,trace_dimension_result
from .contracts import sha256_hex
EXPECTED_DIMENSIONS={"CRL","BRL","TRL","IPRL","TMRL","FRL"}
def validate_dimension_index(rows,*,scope,case_basis_version):
 by={}
 for row in rows:
  d=row.get("dimension_id")
  if d in by:raise ValueError(f"维度重复：{d}")
  by[d]=row
 missing=EXPECTED_DIMENSIONS-set(by)
 if missing or set(by)-EXPECTED_DIMENSIONS:raise ValueError(f"六维缺失或额外：缺失={sorted(missing)}")
 for d,row in by.items():
  if row.get("scope")!=scope:raise ValueError(f"{d} scope不一致")
  if row.get("case_basis_version")!=case_basis_version:raise ValueError(f"{d} CaseBasis版本不一致")
  if row.get("trace_ok") is not True:raise ValueError(f"{d} trace未通过")
 return by
def _payload(blobs,row):return json.loads(blobs.read_bytes(row["result_blob_sha256"]).decode("utf-8"))
def build_offline_dimension_view(case,blobs):
 basis=case.get_case_basis();scope=basis["subject_legal_name"];version=basis["version"];rows=[]
 crl=[dict(r) for r in case._conn.execute("SELECT * FROM crl_dimension_results ORDER BY created_at,result_id")]
 for row in reversed(crl):
  trace=trace_crl_dimension(case,blobs,row["result_id"])
  if trace["ok"]:
   p=_payload(blobs,row);rows.append({"dimension_id":"CRL","scope":row["scope"],"case_basis_version":row["case_basis_version"],"result_id":row["result_id"],"input_digest":row["input_digest"],"product_status":p["dimension"]["product_status"],"attained_level":p["dimension"]["attained_level"],"trace_ok":True});break
 for d in sorted(EXPECTED_DIMENSIONS-{"CRL"}):
  matches=[dict(r) for r in case._conn.execute("SELECT * FROM dimension_results WHERE dimension_id=? ORDER BY created_at,result_id",(d,))]
  for row in reversed(matches):
   trace=trace_dimension_result(case,blobs,row["result_id"])
   if trace["ok"]:
    p=_payload(blobs,row);rows.append({"dimension_id":d,"scope":row["scope"],"case_basis_version":row["case_basis_version"],"scope_id":row["scope_id"],"result_id":row["result_id"],"input_digest":row["input_digest"],"product_status":p["dimension"]["product_status"],"attained_level":p["dimension"]["attained_level"],"trace_ok":True});break
 indexed=validate_dimension_index(rows,scope=scope,case_basis_version=version)
 body={"schema_version":"kth-hybrid.offline-six-dimension-view.v1","status":"offline_candidate","case_basis_version":version,"scope":scope,"dimensions":indexed,"limitations":["非正式KTH评估","非投资决定","无跨维总分","材料不足不等于业务NO"]}
 digest=sha256_hex(json.dumps(body,ensure_ascii=False,sort_keys=True).encode())
 return {**body,"view_id":f"OFFLINE6::{digest}","input_digest":digest}
