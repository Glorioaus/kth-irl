"""夜间TMRL候选：身份不等于当前团队执行能力。"""
from collections import defaultdict
RULE_VERSION="kth-hybrid.tmrl.night.v1"
_IDS=["TMRL1-C1","TMRL1-C2","TMRL2-C1","TMRL2-C2","TMRL2-C3","TMRL3-C1","TMRL3-C2","TMRL3-C3","TMRL4-C1","TMRL4-C2","TMRL4-C3","TMRL4-C4","TMRL4-C5","TMRL5-C1","TMRL5-C2","TMRL5-C3","TMRL5-C4","TMRL5-C5","TMRL6-C1","TMRL6-C2","TMRL6-C3","TMRL6-C4","TMRL6-C5","TMRL7-C1","TMRL7-C2","TMRL7-C3","TMRL7-C4","TMRL7-C5","TMRL8-C1","TMRL8-C2","TMRL8-C3","TMRL8-C4","TMRL8-C5","TMRL9-C1","TMRL9-C2","TMRL9-C3","TMRL9-C4","TMRL9-C5"]
RULE_REQUIREMENTS={cid:cid.lower().replace("-","_")+"_state" for cid in _IDS}
def _unit(v,scope):
 if not isinstance(v,dict) or v.get("subject_scope")!=scope or not all(isinstance(v.get(k),str) and v[k].strip() for k in ("scope_id","subject_scope","unit_kind","unit_label")): raise ValueError("评估单元主体或结构不一致")
 return {k:v[k] for k in ("scope_id","subject_scope","unit_kind","unit_label")}
def _valid(r,c,sid):
 req={"review_id","criterion_id","claim_id","decision","evidence_class","findings","scope_id","reviewer","review_basis","support_scope"}
 return isinstance(r,dict) and req<=set(r) and r["criterion_id"]==c["criterion_id"] and r["scope_id"]==sid and r["decision"] in {"supports","does_not_support"} and r["evidence_class"] in c["eligible_evidence_classes"] and isinstance(r["findings"],dict)
def _support(c,r):
 f=r["findings"]; cid=c["criterion_id"]
 subjects={x for x in f.get("subject_ids",[]) if isinstance(x,str) and x.strip()}
 if f.get(RULE_REQUIREMENTS[cid]) is not True or f.get("team_specific") is not True or not subjects or not isinstance(f.get("current_period"),str) or not f["current_period"].strip(): return False
 if f.get("biography_only") is True or f.get("public_claim_only") is True: return False
 if c["level"]>=2 and not f.get("work_evidence_refs"): return False
 if f.get("relationship_only") is True and f.get("causal_execution_effect") is not True: return False
 if cid=="TMRL4-C2" and (f.get("commitment_evidence") is not True or f.get("capacity_evidence") is not True): return False
 if cid=="TMRL5-C1" and (not isinstance(f.get("operating_record"),str) or not f["operating_record"].strip()): return False
 return True
def _row(c,rs,sid):
 valid=[r for r in rs if _valid(r,c,sid)]; pos=[r for r in valid if r["decision"]=="supports" and _support(c,r)]; neg=[r for r in valid if r["decision"]=="does_not_support"]
 if pos and neg:native,product,why="partial","succeeded","团队证据存在受控冲突。"
 elif neg:native,product,why="not_met","succeeded","受控复核明确不支持该团队准则。"
 elif pos:native,product,why="met","succeeded","当前团队、人员、期间与工作证据已绑定。"
 else:native,product,why="insufficient","insufficient","身份、履历、公开自述或关系本身不能证明当前团队能力。"
 return {**c,"requirements":[RULE_REQUIREMENTS[c["criterion_id"]]],"native_disposition":native,"product_status":product,"review_refs":[r["review_id"] for r in valid],"claim_refs":sorted({r["claim_id"] for r in valid}),"rationale":why,"rule_version":RULE_VERSION}
def evaluate_tmrl_dimension(criteria,reviews,*,scope,assessment_unit):
 if {c.get("criterion_id") for c in criteria}!=set(RULE_REQUIREMENTS):raise ValueError("TMRL准则集合不完整")
 unit=_unit(assessment_unit,scope); grouped=defaultdict(list)
 for r in reviews:
  if isinstance(r,dict) and r.get("criterion_id") in RULE_REQUIREMENTS:grouped[r["criterion_id"]].append(r)
 rows=[_row(c,grouped[c["criterion_id"]],unit["scope_id"]) for c in sorted(criteria,key=lambda x:(x["level"],x["criterion_id"]))];by={r["criterion_id"]:r for r in rows};attained=0;first=None
 for level in range(1,10):
  if all(by[c["criterion_id"]]["native_disposition"]=="met" for c in criteria if c["level"]<=level):attained=level
  else:first=level;break
 return {"dimension":"TMRL","scope":scope,"assessment_unit":unit,"criteria":rows,"attained_level":attained,"first_unmet_level":first,"product_status":"succeeded" if all(r["product_status"]=="succeeded" for r in rows) else "insufficient","rule_version":RULE_VERSION,"method_boundary":"身份overlay强制但不得设定成熟度；关系无因果证据不自动加减分。"}
