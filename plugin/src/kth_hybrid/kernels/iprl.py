"""夜间IPRL候选：批准F/2022 registry的离线受控求值。"""
from collections import defaultdict
RULE_VERSION="kth-hybrid.iprl.night.v1"
RULE_REQUIREMENTS={
"IPRL1-C1":"possible_ip_hypothesized","IPRL1-C2":"ip_ideas_speculative","IPRL1-C3":"ip_documentation_state_recorded","IPRL1-C4":"rights_uncertainty_recorded","IPRL1-C5":"uniqueness_state_of_art_uncertainty_recorded","IPRL2-C1":"ip_forms_mapped","IPRL2-C2":"specific_ip_ideas_identified","IPRL2-C3":"creators_ownership_rights_clarified","IPRL2-C4":"policies_and_contract_restrictions_identified","IPRL3-C1":"key_ip_forms_prioritized","IPRL3-C2":"key_ip_described_for_protection","IPRL3-C3":"prior_art_or_state_of_art_searched","IPRL3-C4":"initial_professional_search","IPRL4-C1":"professional_protectability_confirmed","IPRL4-C2":"protection_priority_business_value_analyzed","IPRL4-C3":"early_filing_made","IPRL5-C1":"draft_ip_strategy","IPRL5-C2":"complete_formal_application_filed","IPRL5-C3":"key_ip_control_agreements","IPRL6-C1":"professional_ip_strategy","IPRL6-C2":"complementary_ip_identified","IPRL6-C3":"initial_fto_assessment","IPRL6-C4":"positive_authority_response","IPRL6-C5":"professional_response_analysis","IPRL7-C1":"national_regional_phase_entered","IPRL7-C2":"complete_fto_assessment","IPRL8-C1":"ip_strategy_and_management_implemented","IPRL8-C2":"key_right_granted","IPRL8-C3":"complementary_filings","IPRL9-C1":"ip_strategy_business_value_proven","IPRL9-C2":"rights_granted_maintained_multiple_countries","IPRL9-C3":"external_ip_access_agreements"}
_RIGHTS={"IPRL2-C3","IPRL5-C3","IPRL8-C2","IPRL9-C2","IPRL9-C3"}
_FTO={"IPRL6-C3","IPRL7-C2"}
def _unit(v,scope):
 if not isinstance(v,dict) or v.get("subject_scope")!=scope or not all(isinstance(v.get(k),str) and v[k].strip() for k in ("scope_id","subject_scope","unit_kind","unit_label")): raise ValueError("评估单元主体或结构不一致")
 return {k:v[k] for k in ("scope_id","subject_scope","unit_kind","unit_label")}
def _valid(r,c,sid):
 req={"review_id","criterion_id","claim_id","decision","evidence_class","findings","scope_id","reviewer","review_basis","support_scope"}
 return isinstance(r,dict) and req<=set(r) and r["criterion_id"]==c["criterion_id"] and r["scope_id"]==sid and r["decision"] in {"supports","does_not_support"} and r["evidence_class"] in c["eligible_evidence_classes"] and isinstance(r["findings"],dict)
def _text(f,*ks): return all(isinstance(f.get(k),str) and f[k].strip() for k in ks)
def _support(c,r,scope):
 cid=c["criterion_id"]; f=r["findings"]
 if f.get(RULE_REQUIREMENTS[cid]) is not True or f.get("ip_specific") is not True or not _text(f,"asset_id"): return False
 if cid in _RIGHTS and (f.get("project_right_binding") is not True or f.get("rightsholder")!=scope): return False
 if cid in _FTO and (not _text(f,"product_configuration","jurisdiction","as_of") or f.get("professional_scope") is not True): return False
 if cid=="IPRL8-C2" and (f.get("granted") is not True or f.get("claim_scope_recorded") is not True or not _text(f,"jurisdiction")): return False
 return True
def _row(c,reviews,sid,scope):
 valid=[r for r in reviews if _valid(r,c,sid)]; pos=[r for r in valid if r["decision"]=="supports" and _support(c,r,scope)]; neg=[r for r in valid if r["decision"]=="does_not_support"]
 if pos and neg: native,product,why="partial","succeeded","IP复核存在受控冲突。"
 elif neg: native,product,why="not_met","succeeded","受控复核明确不支持该IP准则。"
 elif pos: native,product,why="met","succeeded","IP资产、证据类别及准则专属关系已绑定。"
 else: native,product,why="insufficient","insufficient","没有足够IP专属证据；发现、申请、授权、权属与FTO不互相替代。"
 return {**c,"requirements":[RULE_REQUIREMENTS[c["criterion_id"]]],"native_disposition":native,"product_status":product,"review_refs":[r["review_id"] for r in valid],"claim_refs":sorted({r["claim_id"] for r in valid}),"rationale":why,"rule_version":RULE_VERSION}
def _level_ok(level,criteria,by):
 rows=[c for c in criteria if c["level"]<=level]
 required=[c for c in rows if c.get("gate_rule")=="required"]
 if not all(by[c["criterion_id"]]["native_disposition"]=="met" for c in required): return False
 groups={c["gate_rule"] for c in rows if str(c.get("gate_rule","")).startswith("one_of:")}
 return all(any(by[c["criterion_id"]]["native_disposition"]=="met" for c in rows if c.get("gate_rule")==g) for g in groups)
def evaluate_iprl_dimension(criteria,reviews,*,scope,assessment_unit):
 if {c.get("criterion_id") for c in criteria}!=set(RULE_REQUIREMENTS): raise ValueError("IPRL准则集合不完整")
 unit=_unit(assessment_unit,scope); grouped=defaultdict(list)
 for r in reviews:
  if isinstance(r,dict) and r.get("criterion_id") in RULE_REQUIREMENTS: grouped[r["criterion_id"]].append(r)
 rows=[_row(c,grouped[c["criterion_id"]],unit["scope_id"],scope) for c in sorted(criteria,key=lambda x:(x["level"],x["criterion_id"]))]; by={r["criterion_id"]:r for r in rows}; attained=0; first=None
 for level in range(1,10):
  if _level_ok(level,criteria,by): attained=level
  else: first=level; break
 return {"dimension":"IPRL","scope":scope,"assessment_unit":unit,"criteria":rows,"attained_level":attained,"first_unmet_level":first,"product_status":"succeeded" if all(r["product_status"]=="succeeded" for r in rows) else "insufficient","rule_version":RULE_VERSION,"method_boundary":"F/2022内部shadow；非法律意见，保护性不等于权属或FTO。"}
