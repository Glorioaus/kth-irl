"""夜间IPRL候选：32条、optional/one-of及权属边界。"""
import pytest
from kth_hybrid.catalog import build_catalog_from_wheel
from kth_hybrid.kernels.iprl import RULE_REQUIREMENTS, evaluate_iprl_dimension

SCOPE="Company-A"
UNIT={"scope_id":"UNIT-A","subject_scope":SCOPE,"unit_kind":"material_ip_unit","unit_label":"Asset-A"}
CRITERIA=build_catalog_from_wheel()["dimensions"]["IPRL"]["registry"]["criteria"]

def review(c, findings=None, evidence_class=None, decision="supports", suffix="1"):
    evidence_class=evidence_class or c["eligible_evidence_classes"][0]
    f={RULE_REQUIREMENTS[c["criterion_id"]]:True,"ip_specific":True,"asset_id":"ASSET-A"}
    if evidence_class in {"early_filing","formal_application","office_action_record","positive_authority_response","national_regional_phase_record","granted_right","maintained_right","complementary_filing"}: f["record_type"]="official_registry_record"
    if evidence_class in {"executed_assignment","founder_employee_contractor_agreement","license_agreement","ownership_agreement","external_ip_access_agreement"}: f["agreement_status"]="executed_agreement"
    if evidence_class in {"professional_analysis","professional_search","professional_protectability_analysis","professional_ip_strategy","professional_response_analysis","fto_assessment"}: f["analysis_status"]="professional_analysis"
    if c["criterion_id"] in {"IPRL2-C3","IPRL5-C3","IPRL8-C2","IPRL9-C2","IPRL9-C3"}: f.update({"rightsholder":"Company-A","project_right_binding":True})
    if c["criterion_id"] in {"IPRL6-C3","IPRL7-C2"}: f.update({"product_configuration":"CFG-A","jurisdiction":"CN","as_of":"2026-08-27","professional_scope":True})
    if c["criterion_id"]=="IPRL8-C2": f.update({"granted":True,"jurisdiction":"CN","claim_scope_recorded":True,"right_status":"granted_in_force"})
    if c["criterion_id"]=="IPRL9-C2": f.update({"right_status":"maintained_in_force","jurisdictions":["CN","US"],"maintenance_verified":True})
    return {"review_id":f"REV-{c['criterion_id']}-{suffix}","criterion_id":c["criterion_id"],"claim_id":f"C-{c['criterion_id']}-{suffix}","decision":decision,"evidence_class":evidence_class,"findings":f if findings is None else findings,"scope_id":"UNIT-A","reviewer":"night-iprl","review_basis":"合成IP复核","support_scope":"仅支持当前IP准则"}

def evaluate(reviews): return evaluate_iprl_dimension(CRITERIA,reviews,scope=SCOPE,assessment_unit=UNIT)
def row(result,cid): return next(x for x in result["criteria"] if x["criterion_id"]==cid)

@pytest.mark.parametrize("criterion",CRITERIA,ids=lambda x:x["criterion_id"])
def test_each_iprl_rule_has_positive_and_missing_behavior(criterion):
    assert row(evaluate([review(criterion)]),criterion["criterion_id"])["native_disposition"]=="met"
    assert row(evaluate([]),criterion["criterion_id"])["native_disposition"]=="insufficient"

def test_iprl_has_32_unique_requirements():
    assert len(CRITERIA)==len(RULE_REQUIREMENTS)==32
    assert len(set(RULE_REQUIREMENTS.values()))==32

def test_discovered_patent_does_not_prove_project_ownership_grant_or_fto():
    for cid in ("IPRL2-C3","IPRL6-C3","IPRL8-C2"):
        c=next(x for x in CRITERIA if x["criterion_id"]==cid)
        weak={RULE_REQUIREMENTS[cid]:True,"ip_specific":True,"asset_id":"PATENT-X","discovered_patent":True}
        assert row(evaluate([review(c,findings=weak)]),cid)["native_disposition"]=="insufficient"

def test_fto_requires_configuration_jurisdiction_time_and_professional_scope():
    c=next(x for x in CRITERIA if x["criterion_id"]=="IPRL6-C3")
    weak={RULE_REQUIREMENTS["IPRL6-C3"]:True,"ip_specific":True,"asset_id":"ASSET-A","product_configuration":"","jurisdiction":"","as_of":"","professional_scope":False}
    assert row(evaluate([review(c,findings=weak)]),"IPRL6-C3")["native_disposition"]=="insufficient"

def test_optional_rows_do_not_block_and_level6_response_is_one_of():
    through4=[review(c) for c in CRITERIA if c["level"]<=4 and c.get("gate_rule")!="optional"]
    r=evaluate(through4); assert r["attained_level"]==4
    through6=[review(c) for c in CRITERIA if c["level"]<=6 and c.get("gate_rule") not in {"optional","one_of:application_response_resolution"}]
    c4=next(c for c in CRITERIA if c["criterion_id"]=="IPRL6-C4")
    r=evaluate(through6+[review(c4)]); assert r["attained_level"]==6

def test_iprl_unit_scope_is_exact():
    with pytest.raises(ValueError,match="评估单元"):
        evaluate_iprl_dimension(CRITERIA,[],scope=SCOPE,assessment_unit={**UNIT,"subject_scope":"Other"})
