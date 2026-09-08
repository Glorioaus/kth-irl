"""夜间TMRL候选：38条团队能力与身份边界。"""
import pytest
from kth_hybrid.catalog import build_catalog_from_wheel
from kth_hybrid.kernels.tmrl import RULE_REQUIREMENTS,evaluate_tmrl_dimension
SCOPE="Company-A"; UNIT={"scope_id":"UNIT-A","subject_scope":SCOPE,"unit_kind":"material_team_unit","unit_label":"Team-A"}
CRITERIA=build_catalog_from_wheel()["dimensions"]["TMRL"]["registry"]["criteria"]
def review(c,findings=None,evidence_class=None,decision="supports",suffix="1"):
 f={RULE_REQUIREMENTS[c["criterion_id"]]:True,"team_specific":True,"subject_ids":["PERSON-1"],"current_period":"2026-Q3"}
 if c["level"]>=2:f["work_evidence_refs"]=["WORK-1"]
 if c["level"]>=4:f.update({"commitment_evidence":True,"capacity_evidence":True})
 if c["level"]>=5:f["operating_record"]="dated team operating record"
 return {"review_id":f"REV-{c['criterion_id']}-{suffix}","criterion_id":c["criterion_id"],"claim_id":f"C-{c['criterion_id']}-{suffix}","decision":decision,"evidence_class":evidence_class or c["eligible_evidence_classes"][0],"findings":f if findings is None else findings,"scope_id":"UNIT-A","reviewer":"night-tmrl","review_basis":"合成团队复核","support_scope":"仅支持当前团队准则"}
def evaluate(rs):return evaluate_tmrl_dimension(CRITERIA,rs,scope=SCOPE,assessment_unit=UNIT)
def row(r,cid):return next(x for x in r["criteria"] if x["criterion_id"]==cid)
@pytest.mark.parametrize("criterion",CRITERIA,ids=lambda x:x["criterion_id"])
def test_each_tmrl_rule_has_positive_and_missing_behavior(criterion):
 assert row(evaluate([review(criterion)]),criterion["criterion_id"])["native_disposition"]=="met"
 assert row(evaluate([]),criterion["criterion_id"])["native_disposition"]=="insufficient"
def test_tmrl_has_38_unique_requirements():
 assert len(CRITERIA)==len(RULE_REQUIREMENTS)==38 and len(set(RULE_REQUIREMENTS.values()))==38
def test_identity_biography_or_public_claim_does_not_prove_current_capability():
 c=next(x for x in CRITERIA if x["criterion_id"]=="TMRL3-C1")
 weak={RULE_REQUIREMENTS["TMRL3-C1"]:True,"team_specific":True,"subject_ids":["PERSON-1"],"current_period":"2026-Q3","biography_only":True,"work_evidence_refs":[]}
 assert row(evaluate([review(c,findings=weak)]),"TMRL3-C1")["native_disposition"]=="insufficient"
def test_relationship_has_no_readiness_effect_without_causal_evidence():
 c=next(x for x in CRITERIA if x["criterion_id"]=="TMRL4-C5")
 weak={RULE_REQUIREMENTS["TMRL4-C5"]:True,"team_specific":True,"subject_ids":["PERSON-1","PERSON-2"],"current_period":"2026-Q3","relationship_only":True,"work_evidence_refs":["BIO-1"]}
 assert row(evaluate([review(c,findings=weak)]),"TMRL4-C5")["native_disposition"]=="insufficient"
def test_higher_levels_require_current_operating_records():
 c=next(x for x in CRITERIA if x["criterion_id"]=="TMRL5-C1")
 weak=dict(review(c)["findings"]);weak["operating_record"]=""
 assert row(evaluate([review(c,findings=weak)]),"TMRL5-C1")["native_disposition"]=="insufficient"
def test_tmrl_cumulative_and_unit_scope():
 r=evaluate([review(c) for c in CRITERIA if c["level"]==1]);assert r["attained_level"]==1
 with pytest.raises(ValueError,match="评估单元"):evaluate_tmrl_dimension(CRITERIA,[],scope=SCOPE,assessment_unit={**UNIT,"subject_scope":"Other"})
