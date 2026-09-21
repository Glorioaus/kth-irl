"""本地产品闭环的角色等价赋值与有界扫描反例。"""
import json

import pytest

from kth_hybrid.roles import (
    FORBIDDEN_OUTPUT_KEYS,
    _scan_authority,
    assemble_offline_deliberation,
    confirm_role_candidate,
    role_candidate_digest,
    validate_role_attempt,
)
from test_night_roles import LICENSE_ID, VIEW, attempt


ASSIGNMENT_TEXTS = [
    "final_decision：approved",
    "investment_recommendation＝invest",
    '{"final\\u005fdecision":"approved"}',
    '{"investment\\u005frecommendation":"invest"}',
    "Final-Decision = approved",
    "'investment recommendation' : 'invest'",
]


def _candidate(role="PRO", *, target=False, statement=None):
    producer = {"PRO": "P", "CON": "C", "CHAIR": "H"}[role]
    value = attempt(role, producer, f"CTX-{producer}", target=target)
    if statement is not None:
        value["statement"] = statement
        value["candidate_digest"] = role_candidate_digest(value)
    return value


def _rounds(pro_statement="回应", con_statement="回应"):
    return [{
        "round_number": 1,
        "pro_response": {
            "producer_id": "P",
            "observed_candidate_id": "CON-C1",
            "statement": pro_statement,
        },
        "con_response": {
            "producer_id": "C",
            "observed_candidate_id": "PRO-C1",
            "statement": con_statement,
        },
    }]


def _confirmation(candidate, **changes):
    target = candidate["review_target"]
    value = {
        "schema_version": "kth-hybrid.role-confirmation.v2",
        "confirmation_id": "CONF-PRODUCT-OVERNIGHT",
        "candidate_id": candidate["candidate_id"],
        "candidate_digest": candidate["candidate_digest"],
        "input_view_id": VIEW["view_id"],
        "input_digest": VIEW["input_digest"],
        "producer_id": candidate["producer_id"],
        "role": candidate["role"],
        "case_basis_version": VIEW["case_basis_version"],
        "decision": "supports",
        "reviewer": "human",
        "review_basis": "受控复核",
        "evidence_refs": [LICENSE_ID],
        **target,
    }
    value.update(changes)
    return value


def _nested_mapping(levels, leaf="中性内容"):
    value = leaf
    for index in range(levels):
        value = {f"neutral_{index}": value}
    return value


def _nested_array(levels, leaf="中性内容"):
    value = leaf
    for _ in range(levels):
        value = [value]
    return value


def _nested_tuple(levels, leaf="中性内容"):
    value = leaf
    for _ in range(levels):
        value = (value,)
    return value


def _json_encode_layers(value, layers):
    for _ in range(layers):
        value = json.dumps(value, ensure_ascii=False)
    return value


def _json_escape_layers(value, layers):
    for _ in range(layers - 1):
        value = value.replace("\\", "\\\\")
    return value


def _fullwidth_ascii(value):
    return "".join(
        chr(ord(char) + 0xFEE0) if "!" <= char <= "~" else char
        for char in value
    )


def _node_budget_tree(extra_leaf=False):
    widths = [1023, 1023, 1023, 1023 if extra_leaf else 1022]
    return [[None] * width for width in widths]


@pytest.mark.parametrize("role", ["PRO", "CON", "CHAIR"])
@pytest.mark.parametrize("text", ASSIGNMENT_TEXTS)
def test_all_roles_reject_nfkc_and_json_equivalent_assignments(role, text):
    with pytest.raises(ValueError, match="越权"):
        validate_role_attempt(_candidate(role, statement=text), VIEW)


@pytest.mark.parametrize("side", ["pro", "con"])
@pytest.mark.parametrize("text", ASSIGNMENT_TEXTS)
def test_both_round_sides_reject_equivalent_assignments(side, text):
    statements = {"pro_statement": "回应", "con_statement": "回应"}
    statements[f"{side}_statement"] = text
    with pytest.raises(ValueError, match="越权"):
        assemble_offline_deliberation(
            VIEW,
            _candidate("PRO"),
            _candidate("CON"),
            _rounds(**statements),
            _candidate("CHAIR"),
            owner_selected_round_count=1,
        )


@pytest.mark.parametrize("nested_value", [
    {"outer": [{"ＦＩＮＡＬ＿ＤＥＣＩＳＩＯＮ": "approved"}]},
    {"outer": ['{"investment\\u005frecommendation":"invest"}']},
])
def test_review_target_rejects_nested_dict_list_and_json_string(nested_value):
    candidate = _candidate("PRO", target=True)
    candidate["review_target"]["findings"]["comment"] = nested_value
    candidate["candidate_digest"] = role_candidate_digest(candidate)
    with pytest.raises(ValueError, match="越权"):
        validate_role_attempt(candidate, VIEW)


@pytest.mark.parametrize("text", ASSIGNMENT_TEXTS)
def test_confirmation_rejects_equivalent_assignment_in_nested_string(text):
    candidate = validate_role_attempt(_candidate("PRO", target=True), VIEW)
    findings = dict(candidate["review_target"]["findings"])
    findings["nested"] = [text]
    confirmation = _confirmation(candidate, findings=findings)
    with pytest.raises(ValueError, match="越权"):
        confirm_role_candidate(
            candidate, confirmation, dimension_id="BRL", view=VIEW)


@pytest.mark.parametrize("location", ["pro", "round", "confirmation"])
def test_layered_json_string_cannot_hide_equivalent_assignment(location):
    text = json.dumps(
        '{"final\\u005fdecision":"approved"}', ensure_ascii=False)
    if location == "pro":
        with pytest.raises(ValueError, match="越权"):
            validate_role_attempt(_candidate("PRO", statement=text), VIEW)
    elif location == "round":
        with pytest.raises(ValueError, match="越权"):
            assemble_offline_deliberation(
                VIEW,
                _candidate("PRO"),
                _candidate("CON"),
                _rounds(pro_statement=text),
                _candidate("CHAIR"),
                owner_selected_round_count=1,
            )
    else:
        candidate = validate_role_attempt(_candidate("PRO", target=True), VIEW)
        with pytest.raises(ValueError, match="越权"):
            confirm_role_candidate(
                candidate,
                _confirmation(candidate, review_basis=text),
                dimension_id="BRL",
                view=VIEW,
            )


@pytest.mark.parametrize("nested_value", [
    ("中性", {"ＦＩＮＡＬ＿ＤＥＣＩＳＩＯＮ": "approved"}),
    ("中性", '{"investment\\u005frecommendation":"invest"}'),
])
def test_tuple_is_scanned_like_list_for_nested_authority(nested_value):
    candidate = _candidate("PRO", target=True)
    candidate["review_target"]["findings"]["tuple_value"] = nested_value
    candidate["candidate_digest"] = role_candidate_digest(candidate)
    with pytest.raises(ValueError, match="越权"):
        validate_role_attempt(candidate, VIEW)


@pytest.mark.parametrize("key", sorted(FORBIDDEN_OUTPUT_KEYS))
@pytest.mark.parametrize("variant", ["uppercase", "hyphen", "fullwidth"])
def test_all_forbidden_output_keys_use_the_same_canonical_form(key, variant):
    variants = {
        "uppercase": key.upper(),
        "hyphen": key.replace("_", "-"),
        "fullwidth": _fullwidth_ascii(key),
    }
    candidate = _candidate("PRO", target=True)
    candidate["review_target"]["findings"] = {variants[variant]: "forbidden"}
    candidate["candidate_digest"] = role_candidate_digest(candidate)
    with pytest.raises(ValueError, match="越权"):
        validate_role_attempt(candidate, VIEW)


@pytest.mark.parametrize("key", sorted(FORBIDDEN_OUTPUT_KEYS))
def test_all_forbidden_key_names_are_allowed_as_neutral_text_mentions(key):
    text = f"仅讨论{_fullwidth_ascii(key.upper())}字段名，不在此处赋值。"
    returned = validate_role_attempt(_candidate("PRO", statement=text), VIEW)
    assert returned["statement"] == text


@pytest.mark.parametrize("location", ["pro", "round", "confirmation"])
@pytest.mark.parametrize("text", [
    r'前缀 {\"final\u005fdecision\"\u003a\"approved\"} 后缀',
    r'Markdown: `{\"investment\u005frecommendation\"\u003a\"invest\"}`',
])
def test_embedded_json_escapes_cannot_hide_assignment(location, text):
    if location == "pro":
        with pytest.raises(ValueError, match="越权"):
            validate_role_attempt(_candidate("PRO", statement=text), VIEW)
    elif location == "round":
        with pytest.raises(ValueError, match="越权"):
            assemble_offline_deliberation(
                VIEW,
                _candidate("PRO"),
                _candidate("CON"),
                _rounds(con_statement=text),
                _candidate("CHAIR"),
                owner_selected_round_count=1,
            )
    else:
        candidate = validate_role_attempt(_candidate("PRO", target=True), VIEW)
        with pytest.raises(ValueError, match="越权"):
            confirm_role_candidate(
                candidate,
                _confirmation(candidate, review_basis=text),
                dimension_id="BRL",
                view=VIEW,
            )


def test_escaped_neutral_mention_is_allowed_and_kept_verbatim():
    text = r"前缀 final\u005fdecision 只是字段名，后缀"
    returned = validate_role_attempt(_candidate("PRO", statement=text), VIEW)
    assert returned["statement"] == text


@pytest.mark.parametrize("key", [
    r"final\u005fdecision",
    r"attained\u005flevel",
])
def test_dict_keys_decode_json_escapes_before_canonical_check(key):
    candidate = _candidate("PRO", target=True)
    candidate["review_target"]["findings"] = {key: "forbidden"}
    candidate["candidate_digest"] = role_candidate_digest(candidate)
    with pytest.raises(ValueError, match="越权"):
        validate_role_attempt(candidate, VIEW)


def test_four_json_escape_rounds_are_allowed_for_neutral_text():
    text = _json_escape_layers(
        r"final\u005fdecision 只是字段名，不在此处赋值。", 4)
    returned = validate_role_attempt(_candidate("PRO", statement=text), VIEW)
    assert returned["statement"] == text


@pytest.mark.parametrize("location", ["text", "dict_key"])
def test_fifth_json_escape_round_fails_closed(location):
    escaped = _json_escape_layers(r"neutral\u005fmention", 5)
    if location == "text":
        candidate = _candidate("PRO", statement=escaped)
    else:
        candidate = _candidate("PRO", target=True)
        candidate["review_target"]["findings"] = {escaped: "neutral"}
        candidate["candidate_digest"] = role_candidate_digest(candidate)
    with pytest.raises(ValueError, match="JSON转义.*4"):
        validate_role_attempt(candidate, VIEW)


def test_dict_key_length_budget_accepts_256_and_rejects_257():
    _scan_authority({"k" * 256: None})
    with pytest.raises(ValueError, match="键长度.*256"):
        _scan_authority({"k" * 257: None})


def test_total_string_budget_accepts_262144_and_rejects_262145():
    chunks = ["x" * 65536] * 4
    _scan_authority(chunks)
    with pytest.raises(ValueError, match="累计字符串字符.*262144"):
        _scan_authority([*chunks, "x"])


def test_node_budget_accepts_4096_and_rejects_4097():
    _scan_authority(_node_budget_tree())
    with pytest.raises(ValueError, match="节点数.*4096"):
        _scan_authority(_node_budget_tree(extra_leaf=True))


@pytest.mark.parametrize("container", ["list", "tuple", "dict"])
def test_container_width_accepts_1024_and_rejects_1025(container):
    def build(width):
        if container == "dict":
            return {f"k{index}": None for index in range(width)}
        values = [None] * width
        return tuple(values) if container == "tuple" else values

    _scan_authority(build(1024))
    with pytest.raises(ValueError, match="容器宽度.*1024"):
        _scan_authority(build(1025))


@pytest.mark.parametrize("text", [
    "讨论ＦＩＮＡＬ＿ＤＥＣＩＳＩＯＮ字段的命名。",
    "investment recommendation 只是字段名，不在此处赋值。",
    "The final decision remains outside this role.",
])
def test_neutral_mentions_are_allowed_and_original_text_is_unchanged(text):
    candidate = _candidate("PRO", statement=text)
    returned = validate_role_attempt(candidate, VIEW)
    assert returned["statement"] == text
    assert returned == candidate


def test_round_and_confirmation_keep_original_text_unchanged():
    round_text = "回应中仅讨论ＦＩＮＡＬ＿ＤＥＣＩＳＩＯＮ字段。"
    result = assemble_offline_deliberation(
        VIEW,
        _candidate("PRO"),
        _candidate("CON"),
        _rounds(pro_statement=round_text),
        _candidate("CHAIR"),
        owner_selected_round_count=1,
    )
    assert result["rounds"][0]["pro_response"]["statement"] == round_text

    candidate = validate_role_attempt(_candidate("PRO", target=True), VIEW)
    basis = "逐字保留ＦＩＮＡＬ＿ＤＥＣＩＳＩＯＮ字段说明。"
    with pytest.raises(ValueError, match="legacy_restricted"):
        confirm_role_candidate(
            candidate,
            _confirmation(candidate, review_basis=basis),
            dimension_id="BRL",
            view=VIEW,
        )


def test_text_length_limit_accepts_65536_and_rejects_65537():
    at_limit = "界" * 65536
    returned = validate_role_attempt(
        _candidate("PRO", statement=at_limit), VIEW)
    assert returned["statement"] == at_limit

    over_limit = "界" * 65537
    with pytest.raises(ValueError, match="长度.*65536"):
        validate_role_attempt(_candidate("PRO", statement=over_limit), VIEW)


def test_direct_structure_depth_accepts_16_and_rejects_17():
    at_limit = _candidate("PRO", target=True)
    at_limit["review_target"]["findings"] = _nested_mapping(14)
    at_limit["candidate_digest"] = role_candidate_digest(at_limit)
    assert validate_role_attempt(at_limit, VIEW)["review_target"]["findings"] \
        == at_limit["review_target"]["findings"]

    over_limit = _candidate("PRO", target=True)
    over_limit["review_target"]["findings"] = _nested_mapping(15)
    over_limit["candidate_digest"] = role_candidate_digest(over_limit)
    with pytest.raises(ValueError, match="结构深度.*16"):
        validate_role_attempt(over_limit, VIEW)


def test_complete_json_depth_counts_context_and_stops_at_16():
    at_limit = json.dumps(_nested_array(15), ensure_ascii=False)
    assert validate_role_attempt(
        _candidate("PRO", statement=at_limit), VIEW)["statement"] == at_limit

    over_limit = json.dumps(_nested_array(16), ensure_ascii=False)
    with pytest.raises(ValueError, match="结构深度.*16"):
        validate_role_attempt(_candidate("PRO", statement=over_limit), VIEW)


def test_layered_json_string_depth_is_bounded_and_counts_context():
    at_limit = _json_encode_layers("", 14)
    assert validate_role_attempt(
        _candidate("PRO", statement=at_limit), VIEW)["statement"] == at_limit

    over_limit = _json_encode_layers("", 15)
    with pytest.raises(ValueError, match="结构深度.*16"):
        validate_role_attempt(_candidate("PRO", statement=over_limit), VIEW)


def test_tuple_structure_depth_accepts_16_and_rejects_17():
    at_limit = _candidate("PRO", target=True)
    at_limit["review_target"]["findings"] = {
        "nested": _nested_tuple(13),
    }
    at_limit["candidate_digest"] = role_candidate_digest(at_limit)
    assert validate_role_attempt(at_limit, VIEW)["review_target"]["findings"] \
        == at_limit["review_target"]["findings"]

    over_limit = _candidate("PRO", target=True)
    over_limit["review_target"]["findings"] = {
        "nested": _nested_tuple(14),
    }
    over_limit["candidate_digest"] = role_candidate_digest(over_limit)
    with pytest.raises(ValueError, match="结构深度.*16"):
        validate_role_attempt(over_limit, VIEW)


def test_excessive_structure_is_explicitly_rejected_without_recursion_error():
    candidate = _candidate("PRO", target=True)
    candidate["review_target"]["findings"] = _nested_mapping(2000)
    with pytest.raises(ValueError, match="结构深度.*16"):
        validate_role_attempt(candidate, VIEW)
