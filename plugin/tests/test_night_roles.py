"""夜间离线角色v2合同。"""
import pytest
from kth_hybrid.roles import (assemble_offline_deliberation,
                              confirm_role_candidate, role_candidate_digest,
                              validate_role_attempt)

LICENSE={"license_id":"LIC-1","dimension_id":"BRL","result_id":"BRL-R",
         "claim_id":"CLAIM-1","quote_sha256":"a"*64,
         "evidence_class":"business_concept","subject_scope":"Company-A",
         "scope_id":"UNIT-A","qualification_view_digest":"b"*64}
VIEW={"schema_version":"kth-hybrid.offline-six-dimension-view.v2",
      "view_id":"OFFLINE6::abc","input_digest":"abc","scope":"Company-A",
      "case_basis_version":1,
      "dimensions":{d:{"result_id":d+"-R","scope_id":"UNIT-A"}
                    for d in ("CRL","BRL","TRL","IPRL","TMRL","FRL")},
      "evidence_licenses":{"LIC-1":LICENSE}}

def attempt(role,producer,context,target=False):
 value={"schema_version":"kth-hybrid.offline-role-attempt.v2","role":role,
        "producer_id":producer,"context_id":context,"simulated":True,
        "input_view_id":VIEW["view_id"],"input_digest":VIEW["input_digest"],
        "scope":VIEW["scope"],"dimension_result_refs":{d:r["result_id"] for d,r in VIEW["dimensions"].items()},
        "candidate_id":role+"-C1","statement":"仅陈述证据边界。",
        "evidence_refs":["LIC-1"],"limitations":["离线模拟"]}
 if target:
  value["review_target"]={"dimension_id":"BRL","criterion_id":"BRL1-BM",
                           "claim_id":"CLAIM-1","quote_sha256":"a"*64,
                           "evidence_class":"business_concept",
                           "subject_scope":"Company-A","scope_id":"UNIT-A",
                           "support_scope":"仅支持BRL1-BM",
                           "findings":{"business_idea_or_model_stated":True}}
 value["candidate_digest"]=role_candidate_digest(value)
 return value

def test_role_attempt_binds_exact_view_and_all_dimensions():
 assert validate_role_attempt(attempt("PRO","P","CTX-P"),VIEW)["candidate_id"]=="PRO-C1"
 for change in ({"input_digest":"wrong"},{"scope":"Other"},{"dimension_result_refs":{"CRL":"CRL-R"}}):
  value={**attempt("PRO","P","CTX-P"),**change};value["candidate_digest"]=role_candidate_digest(value)
  with pytest.raises(ValueError):validate_role_attempt(value,VIEW)

def test_role_cannot_write_level_disposition_or_final_decision():
 for key in ("attained_level","native_disposition","final_decision","investment_recommendation"):
  with pytest.raises(ValueError,match="越权"):validate_role_attempt({**attempt("PRO","P","CTX-P"),key:1},VIEW)

def test_pro_con_are_independent_and_rounds_are_exact():
 pro=attempt("PRO","P","CTX-P");con=attempt("CON","C","CTX-C")
 round1={"round_number":1,"pro_response":{"producer_id":"P","observed_candidate_id":"CON-C1","statement":"回应"},"con_response":{"producer_id":"C","observed_candidate_id":"PRO-C1","statement":"回应"}}
 chair=attempt("CHAIR","H","CTX-H")
 result=assemble_offline_deliberation(VIEW,pro,con,[round1],chair,owner_selected_round_count=1)
 assert result["status"]=="simulated_offline_candidate" and "decision" not in result
 bad={**con,"producer_id":"P"};bad["candidate_digest"]=role_candidate_digest(bad)
 with pytest.raises(ValueError,match="独立"):assemble_offline_deliberation(VIEW,pro,bad,[round1],chair,owner_selected_round_count=1)

def test_candidate_requires_controlled_confirmation_before_review_adapter():
 candidate=validate_role_attempt(attempt("PRO","P","CTX-P",target=True),VIEW)
 target=candidate["review_target"]
 confirmation={"schema_version":"kth-hybrid.role-confirmation.v2",
               "confirmation_id":"CONF-1","candidate_id":candidate["candidate_id"],
               "candidate_digest":candidate["candidate_digest"],
               "input_view_id":VIEW["view_id"],"input_digest":VIEW["input_digest"],
               "producer_id":"P","role":"PRO","case_basis_version":1,
               "decision":"supports","reviewer":"human","review_basis":"受控复核",
               "evidence_refs":["LIC-1"],**target}
 review=confirm_role_candidate(candidate,confirmation,dimension_id="BRL",view=VIEW)
 assert review["decision"]=="supports" and "native_disposition" not in review
 with pytest.raises(ValueError):confirm_role_candidate(candidate,{**confirmation,"candidate_id":"OTHER"},dimension_id="BRL",view=VIEW)
