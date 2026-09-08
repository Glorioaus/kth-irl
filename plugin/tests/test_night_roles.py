"""夜间离线角色合同：模拟候选不等于被测模型运行或最终处置。"""
import pytest
from kth_hybrid.roles import validate_role_attempt,assemble_offline_deliberation,confirm_role_candidate

VIEW={"view_id":"OFFLINE6::abc","input_digest":"abc","scope":"Company-A","case_basis_version":1,"dimensions":{d:{"result_id":d+"-R","scope_id":"UNIT-A"} for d in ("CRL","BRL","TRL","IPRL","TMRL","FRL")}}
def attempt(role,producer,context):
 return {"schema_version":"kth-hybrid.offline-role-attempt.v1","role":role,"producer_id":producer,"context_id":context,"simulated":True,"input_view_id":"OFFLINE6::abc","input_digest":"abc","scope":"Company-A","dimension_result_refs":{d:d+"-R" for d in VIEW["dimensions"]},"candidate_id":role+"-C1","statement":"候选分析，不形成成熟度或决定。","evidence_refs":["CLAIM-1"],"limitations":["离线合成输出"]}
def test_role_attempt_binds_exact_view_and_all_dimensions():
 assert validate_role_attempt(attempt("PRO","P","CTX-P"),VIEW)["candidate_id"]=="PRO-C1"
 for change in ({"input_digest":"wrong"},{"scope":"Other"},{"dimension_result_refs":{"CRL":"CRL-R"}}):
  with pytest.raises(ValueError):validate_role_attempt({**attempt("PRO","P","CTX-P"),**change},VIEW)
def test_role_cannot_write_level_disposition_or_final_decision():
 for key in ("attained_level","native_disposition","final_decision","investment_recommendation"):
  with pytest.raises(ValueError,match="越权"):validate_role_attempt({**attempt("PRO","P","CTX-P"),key:1},VIEW)
def test_pro_con_are_independent_and_rounds_are_exact():
 pro=attempt("PRO","P","CTX-P");con=attempt("CON","C","CTX-C")
 round1={"round_number":1,"pro_response":{"producer_id":"P","observed_candidate_id":"CON-C1","statement":"回应"},"con_response":{"producer_id":"C","observed_candidate_id":"PRO-C1","statement":"回应"}}
 chair=attempt("CHAIR","H","CTX-H")
 result=assemble_offline_deliberation(VIEW,pro,con,[round1],chair,owner_selected_round_count=1)
 assert result["status"]=="simulated_offline_candidate" and "decision" not in result
 with pytest.raises(ValueError,match="独立"):assemble_offline_deliberation(VIEW,pro,{**con,"producer_id":"P"},[round1],chair,owner_selected_round_count=1)
def test_candidate_requires_controlled_confirmation_before_review_adapter():
 candidate=validate_role_attempt(attempt("PRO","P","CTX-P"),VIEW)
 confirmation={"confirmation_id":"CONF-1","candidate_id":"PRO-C1","decision":"confirmed","reviewer":"human-offline","review_basis":"受控合成复核","criterion_id":"BRL1-BM","claim_id":"CLAIM-1","quote_sha256":"a"*64,"evidence_class":"business_concept","scope_id":"UNIT-A","subject_scope":"Company-A","support_scope":"仅支持BRL1-BM","findings":{"business_idea_or_model_stated":True}}
 review=confirm_role_candidate(candidate,confirmation,dimension_id="BRL")
 assert review["decision"]=="supports" and "native_disposition" not in review
 with pytest.raises(ValueError):confirm_role_candidate(candidate,{**confirmation,"candidate_id":"OTHER"},dimension_id="BRL")
