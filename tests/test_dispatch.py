"""SIGIL dispatch policy over committed operation/authority/reservation/state facts."""
import json
import pytest

from api_support import credential
from conftest import SIGIL_ROOT, needs_toolchain
from dispatch_support import change_snapshot, effect, initial, proposal
from scripts.compose_application import compose_application
from turn_support import Decision, fields, record, refused, run, tool_reply


@pytest.fixture(scope="module")
def program():
    needs_toolchain()
    return compose_application("dispatch", SIGIL_ROOT).text


def test_model_dispatch_reuses_exact_intent_body_and_original_authority(mcp, program):
    values = initial(mcp)
    result = proposal(mcp, program, values)
    intent = fields(fields(values[7], "SR1\n", 3)[2], "SI1\n", 5)
    assert result[0] == "provider"
    assert result[1].endswith("|" + intent[4])
    assert "{{secret:anthropic}}" in result[1]
    assert result[2:5] == ["a.intent", "a" * 64 + ":1", "1"]
    assert fields(result[5], "TG1\n", 2) == ["100", "270"]
    assert result[6:] == ["a" * 64, "1", "alice", "tenant-a", "b" * 64]


@pytest.fixture(scope="module")
def model_view(mcp):
    return initial(mcp)


def deny(mcp, program, values, code):
    refused(mcp, program, record("DF1\n", values), code)


@pytest.mark.parametrize("kwargs", [{"principal": "bob"}, {"tenant": "other"}, {"epoch": "new"},
    {"scopes": []}, {"tools": []}, {"tools": ["*"]}, {"prefix": "other"},
    {"turn_seconds": 121}, {"before": 152}, {"expires": 151}])
def test_current_authority_cannot_replace_original_scope_or_extend_eligibility(mcp, program, model_view, kwargs):
    values = list(model_view)
    values[0] = credential(**kwargs)["facts"]
    deny(mcp, program, values, 403)


def test_equivalent_rotation_preserves_original_deadline_and_intersects_active_window(mcp, program, model_view):
    values = list(model_view)
    values[0] = credential(before=151, expires=200)["facts"]
    assert fields(proposal(mcp, program, values)[5], "TG1\n", 2) == ["151", "200"]
    values[0] = credential(expires=9000000001)["facts"]
    assert fields(proposal(mcp, program, values)[5], "TG1\n", 2) == ["100", "270"]


@pytest.mark.parametrize("allowed", [[], ["not_in_catalog"], ["*"], ["read_file"]])
def test_model_payload_uses_the_admitted_filtered_catalog(mcp, program, allowed):
    values = initial(mcp, row=credential(tools=allowed))
    selected = proposal(mcp, program, values)
    expected = fields(fields(values[7], "SR1\n", 3)[2], "SI1\n", 5)[4]
    assert selected[1].endswith("|" + expected)


@pytest.mark.parametrize("at,code", [(149, 409), (270, 408), (271, 408)])
def test_clock_and_operation_expiry_prevent_dispatch(mcp, program, model_view, at, code):
    values = list(model_view)
    values[3] = str(at)
    deny(mcp, program, values, code)


@pytest.mark.parametrize("coordinate", range(4, 9))
@pytest.mark.parametrize("snapshot", [record("SR1\n", ["ok", "0", ""]),
    record("SR1\n", ["ok", "2", ""]), record("SR1\n", ["error", "0", ""]),
    record("SR1\n", ["ok", "01", "forged"])])
def test_all_five_retained_records_are_required_and_canonical(mcp, program, model_view, coordinate, snapshot):
    values = list(model_view)
    values[coordinate] = snapshot
    deny(mcp, program, values, 409)


@pytest.mark.parametrize("index,marker,field,value", [
    (4, "OQ2\n", 0, "bob"), (4, "OQ2\n", 1, "other"), (4, "OQ2\n", 2, "other-key"),
    (4, "OQ2\n", 4, "e" * 64), (4, "OQ2\n", 5, "other-epoch"), (4, "OQ2\n", 6, "99"),
    (4, "OQ2\n", 7, "271"), (4, "OQ2\n", 9, "done"), (4, "OQ2\n", 10, "e" * 64),
    (5, "BR1\n", 0, "e" * 64), (5, "BR1\n", 1, "other"), (5, "BR1\n", 2, "bob"),
    (5, "BR1\n", 4, "19999"), (5, "BR1\n", 5, "4095"), (5, "BR1\n", 6, "271"),
    (5, "BR1\n", 7, "settled"), (5, "BR1\n", 8, "e" * 64),
    (6, "PT1\n", 0, "e" * 64), (6, "PT1\n", 2, "2"), (6, "PT1\n", 13, "271"),
    (6, "PT1\n", 15, "other-session"), (6, "PT1\n", 16, "other-key"),
    (7, "SI1\n", 0, "e" * 64), (7, "SI1\n", 1, "2"), (7, "SI1\n", 2, "tool"),
    (7, "SI1\n", 4, "{\"model\":\"forged\"}"),
    (8, "BH1\n", 0, "other"), (8, "BH1\n", 2, "0"), (8, "BH1\n", 2, "9"),
    (8, "BH1\n", 3, "19999"), (8, "BH1\n", 3, "160001"),
    (8, "BH1\n", 4, "4095"), (8, "BH1\n", 4, "32769"),
])
def test_operation_reservation_state_intent_and_counter_bindings_are_not_interchangeable(
        mcp, program, model_view, index, marker, field, value):
    values = list(model_view)
    change_snapshot(values, index, marker, field, value)
    deny(mcp, program, values, 409)


@pytest.mark.parametrize("index,value", [(0, "other"), (1, "other"), (2, "e" * 64),
    (3, "e" * 64), (4, '["127.0.0.1","*"]'), (5, '["/tmp"]'), (6, '["anthropic","other"]')])
def test_worker_metadata_cannot_widen_or_replace_the_bound_effect(mcp, program, model_view, index, value):
    values = list(model_view)
    actual = fields(values[10], "EF1\n", 7)
    actual[index] = value
    # Alias is a host lookup identity, not a hardcoded pi name. Test an invalid alias.
    if index == 0:
        actual[0] = "bad/name"
    values[10] = record("EF1\n", actual)
    deny(mcp, program, values, 400 if index == 1 else 403)


@pytest.mark.parametrize("index,value", [(0, "other"), (1, "read_file"), (2, "e" * 64),
    (3, "e" * 64), (4, 'http://127.0.0.1:1234/|injected'), (5, "*"), (6, "other")])
def test_operator_role_binding_must_match_actual_worker_and_authorized_tenant(mcp, program, model_view, index, value):
    values = list(model_view)
    actual = fields(values[10], "EF1\n", 7)
    bound = fields(actual[1], "EB1\n", 7)
    bound[index] = value
    actual[1] = record("EB1\n", bound)
    values[10] = record("EF1\n", actual)
    deny(mcp, program, values, 403)


@pytest.mark.parametrize("url,host", [('https://other.test/messages', 'api.test'),
    ('https://api.test.evil/messages', 'api.test'), ('https://api.test@evil/messages', 'api.test'),
    ('http://api.test/messages', 'api.test'), ('https://api.test:0/messages', 'api.test'),
    ('https://api.test:65536/messages', 'api.test'), ('https://api.test:080/messages', 'api.test'),
    ('https://api.test/messages', '*')])
def test_provider_destination_is_bound_to_one_host_with_bounded_canonical_port(mcp, program, model_view, url, host):
    values = list(model_view)
    values[10] = effect(argument=url, grant=host)
    deny(mcp, program, values, 403)


@pytest.mark.parametrize("index,value", [(12, "0"), (10, "20000"), (11, "3073")])
def test_unknown_or_exhausted_reported_model_usage_cannot_be_assumed_free(mcp, program, model_view, index, value):
    values = list(model_view)
    change_snapshot(values, 6, "PT1\n", index, value)
    deny(mcp, program, values, 429)


def tool_view(mcp, values, *, name="read_file", path="README.md", usage=None):
    values = list(values)
    state = fields(values[6], "SR1\n", 3)[2]
    intent = fields(fields(values[7], "SR1\n", 3)[2], "SI1\n", 5)
    first = Decision(state, "model", "1", "", intent[4])
    response = json.loads(tool_reply(name, usage=usage))
    response["content"][0]["input"]["path"] = path
    next_step = run(mcp, compose_application("turn", SIGIL_ROOT).text, first.result(json.dumps(response), now=151))
    assert next_step.action == "tool"
    values[6] = record("SR1\n", ["ok", "2", next_step.state])
    values[7] = record("SR1\n", ["ok", "1", record("SI1\n", [values[2], next_step.sequence,
        "tool", name, next_step.input])])
    values[9] = values[2] + ":" + next_step.sequence
    values[10] = effect(alias="file", role="read_file", argument="/permitted", grant="/permitted", secret="")
    return values


@pytest.mark.parametrize("path", ["README.md", "a|b.md", "two words.md", "../outside.md", "/elsewhere.md", "é😀.md"])
def test_file_dispatch_uses_existing_argument_codec_and_leaves_scope_enforcement_to_worker(
        mcp, program, model_view, path):
    values = tool_view(mcp, model_view, path=path)
    selected = proposal(mcp, program, values)
    assert selected[0] == "file"
    assert selected[1] == (path if path.startswith("/") else "/permitted/" + path)
    assert selected[3] == values[2] + ":2"


def test_tool_input_must_match_the_current_pending_tool(mcp, program, model_view):
    values = tool_view(mcp, model_view)
    change_snapshot(values, 7, "SI1\n", 4, '{"path":"different"}')
    deny(mcp, program, values, 409)


def test_exact_reported_limits_preserve_remaining_output_reservation(mcp, program, model_view):
    values = list(model_view)
    change_snapshot(values, 6, "PT1\n", 10, "19999")
    change_snapshot(values, 6, "PT1\n", 11, "3072")
    assert proposal(mcp, program, values)[0] == "provider"


def test_terminal_state_has_no_effect_proposal(mcp, program, model_view):
    values = list(model_view)
    change_snapshot(values, 6, "PT1\n", 1, "done")
    change_snapshot(values, 7, "SI1\n", 2, "done")
    deny(mcp, program, values, 403)
