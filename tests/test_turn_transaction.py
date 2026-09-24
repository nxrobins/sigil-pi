"""Real SIGIL owns transaction membership, minimal intents and delivery correlation."""

import json

import pytest

from conftest import PI_ROOT, SIGIL_ROOT, needs_toolchain
from scripts.compose_application import compose_application
from store_support import NativeStore, mutation
from transaction_support import observation, plan, snapshot
from turn_support import FUEL, event, fields, record, refused, run, source, submission, text_reply, tool_reply


@pytest.fixture(scope="module")
def program():
    needs_toolchain()
    return compose_application("turn_transaction", SIGIL_ROOT).text


def test_sigil_emits_exact_atomic_start_request_without_python_batch_policy(mcp, program):
    batch = plan(mcp, program, event())
    expected = run(mcp, source(), event())
    request = batch.request
    assert set(request) == {"op", "checks", "writes"}
    assert request["op"] == "commit" and request["checks"] == []
    assert request["writes"] == [
        mutation("pi-a.state", "conversation", expected.state),
        mutation("pi-a.intent", "operation-1:1", record("SI1\n", [
            "operation-1", "1", "model", "", expected.input]))]


def test_observation_drives_the_same_reducer_and_only_minimal_tool_input_is_published(mcp, program):
    first = plan(mcp, program, event()).decision
    incoming = first.result(tool_reply("read_file"))
    following = plan(mcp, program, incoming, 1, delivery=observation(incoming))
    expected = run(mcp, source(), incoming)
    assert following.decision == expected
    assert following.request["checks"] == [{"namespace": "pi-a.delivery", "key": "operation-1:1", "revision": 1}]
    intent = fields(following.request["writes"][1]["value"], "SI1\n", 5)
    assert intent[:4] == ["operation-1", "2", "tool", "read_file"]
    assert json.loads(intent[4]) == {"path": "README.md"}
    assert first.state not in following.request["writes"][1]["value"]


@pytest.mark.parametrize("kind,phase", [("ok", "done"), ("unknown", "uncertain"),
    ("cancelled_unsent", "cancelled"), ("expired_unsent", "failed"), ("error", "failed")])
def test_terminal_interpretation_has_no_next_intent(mcp, program, kind, phase):
    first = plan(mcp, program, event()).decision
    incoming = first.result(text_reply("Retained answer") if kind == "ok" else "", kind=kind)
    final = plan(mcp, program, incoming, 1, delivery=observation(incoming))
    assert final.decision.action == phase
    assert len(final.request["writes"]) == len(final.request["checks"]) == 1


@pytest.mark.parametrize("index,replacement", [(1, "another-op:1"), (1, "operation-1:2"), (4, "4"), (6, text_reply("changed"))])
def test_observation_operation_sequence_kind_and_payload_must_match_exactly(mcp, program, index, replacement):
    first = plan(mcp, program, event()).decision
    incoming = first.result(text_reply())
    delivered = fields(observation(incoming), "DR1\n", 7)
    delivered[index] = replacement
    # An unknown observation cannot contain a body. Make it structurally valid
    # while still disagreeing with the event so correlation, not grammar, rejects.
    if index == 4:
        delivered[5] = delivered[6] = ""
    refused(mcp, program, snapshot(incoming, 1, delivery=record("DR1\n", delivered)), 409)


@pytest.mark.parametrize("revision", ["", "00", "01", "-1", "+1", "1.0", "1e0", " 1", "1 ",
                                         "9223372036854775808", "99999999999999999999"])
def test_revision_encoding_is_exact_and_bounded(mcp, program, revision):
    values = fields(snapshot(event()), "PX2\n", 8)
    values[4] = revision
    refused(mcp, program, record("PX2\n", values), 400)


@pytest.mark.parametrize("revision", [9007199254740993, 9223372036854775807])
def test_large_native_revisions_are_not_rounded_through_json_floats(mcp, program, revision):
    first = plan(mcp, program, event()).decision
    incoming = first.result(text_reply())
    final = plan(mcp, program, incoming, revision, delivery=observation(incoming), delivery_revision=revision)
    assert final.request["checks"][0]["revision"] == revision
    assert final.request["writes"][0]["revision"] == revision


@pytest.mark.parametrize("bad", ["", "a/b", "a\\b", "a\n", "é", "a" * 129, 'a"', "a b"])
def test_namespace_coordinates_cannot_inject_storage_requests(mcp, program, bad):
    values = fields(snapshot(event()), "PX2\n", 8)
    values[0] = bad
    refused(mcp, program, record("PX2\n", values), 400)


@pytest.mark.parametrize("left,right", [(0, 1), (0, 2), (1, 2)])
def test_domain_intent_and_delivery_namespaces_must_be_disjoint(mcp, program, left, right):
    values = fields(snapshot(event()), "PX2\n", 8)
    values[left] = values[right]
    refused(mcp, program, record("PX2\n", values), 400)


@pytest.mark.parametrize("delivery,revision", [("bad", 0), ("", 1), ("bad", 1)])
def test_start_cannot_smuggle_a_delivery_precondition(mcp, program, delivery, revision):
    refused(mcp, program, snapshot(event(), delivery=delivery, delivery_revision=revision), 409)


def test_state_presence_revision_and_session_coordinate_must_agree(mcp, program):
    refused(mcp, program, snapshot(event(), 1), 409)
    refused(mcp, program, snapshot(event(), key="different-session"), 409)
    first = plan(mcp, program, event()).decision
    incoming = first.result(text_reply())
    refused(mcp, program, snapshot(incoming, 0, delivery=observation(incoming)), 409)
    refused(mcp, program, snapshot(incoming, 1, delivery=observation(incoming), delivery_revision=0), 409)
    refused(mcp, program, snapshot(incoming, 1, delivery="", delivery_revision=1), 400)


@pytest.mark.parametrize("bad", ["", "PX2\n", "PX2\n00000099x", "PX2\n-0000001x"])
def test_malformed_snapshot_has_no_request_output(mcp, program, bad):
    refused(mcp, program, bad, 400)


def test_extra_frames_and_trailing_bytes_are_not_ignored(mcp, program):
    refused(mcp, program, snapshot(event()) + "extra", 400)
    values = fields(snapshot(event()), "PX2\n", 8)
    refused(mcp, program, record("PX2\n", values + ["grant-all"]), 400)


def test_observation_grammar_rejects_unknown_versions_kinds_and_ambiguous_empty_facts(mcp, program):
    first = plan(mcp, program, event()).decision
    incoming = first.result(kind="unknown")
    valid = ["pi-a.intent", "operation-1:1", "1", "executor-1", "4", "", ""]
    malformed = [record("DR2\n", valid), record("DR1\n", valid + ["extra"]),
                 record("DR1\n", valid[:4] + ["0", "", ""]),
                 record("DR1\n", valid[:4] + ["trusted-success", "", ""]),
                 record("DR1\n", valid[:6] + ["not empty"])]
    for bad in malformed:
        refused(mcp, program, snapshot(incoming, 1, delivery=bad), 400)


@pytest.mark.parametrize("phase", ["model", "tool"])
def test_pi_interprets_generic_observed_failure_using_its_own_phase(mcp, program, phase):
    current = plan(mcp, program, event()).decision
    revision = 1
    if phase == "tool":
        model_event = current.result(tool_reply("read_file"))
        current = plan(mcp, program, model_event, revision, delivery=observation(model_event)).decision
        revision += 1
    kind = "error" if phase == "model" else "tool_error"
    incoming = current.result("observed effect failure", kind=kind)
    delivered = record("DR1\n", ["pi-a.intent", f"operation-1:{current.sequence}", "1", "executor-1",
                                 "2", "failed", "observed effect failure"])
    following = plan(mcp, program, incoming, revision, delivery=delivered)
    assert following.decision.action == ("failed" if phase == "model" else "model")
    if phase == "tool":
        result = json.loads(following.decision.input)["messages"][-1]["content"][0]
        assert result["content"] == "observed effect failure" and result["is_error"] is True


def test_observation_namespace_and_immutable_pi_intent_revision_must_match(mcp, program):
    current = plan(mcp, program, event()).decision
    incoming = current.result(text_reply())
    original = fields(observation(incoming), "DR1\n", 7)
    for index, replacement in ((0, "pi-b.intent"), (2, "2"), (2, "01")):
        values = original.copy()
        values[index] = replacement
        refused(mcp, program, snapshot(incoming, 1, delivery=record("DR1\n", values)), 409)
    refused(mcp, program, snapshot(event()).replace("PX2\n", "PX1\n", 1), 400)
    refused(mcp, program, snapshot(incoming, 1, delivery=record("SO1\n", ["operation-1", "1", "ok", text_reply()])), 400)


def test_unicode_and_control_bytes_roundtrip_through_sigil_json_and_native_storage(
        mcp, program, native_store_binary, tmp_path):
    message = 'Unicode 😀 café\n"quoted"\\slash\x00\t'
    batch = plan(mcp, program, event(payload=submission(message)))
    with NativeStore(native_store_binary, tmp_path / "records", {
            "pi-a.state": "read_write", "pi-a.intent": "create_only", "pi-a.delivery": "read"}, initialize=True) as store:
        batch.commit(store)
        saved = store.get("pi-a.state", "conversation")
        history = json.loads(fields(saved["value"], "PT1\n", 17)[5])
        assert history[0]["content"] == message


def test_escaped_transaction_size_refuses_without_partial_output(mcp, program):
    # Valid submission and reducer input, but nested JSON quoting exceeds the
    # producer's 2 MiB output ceiling. No prefix/partial commit may escape.
    refused(mcp, program, snapshot(event(payload=submission("\x00" * 150000))), 413)


def test_native_guest_memory_ceiling_emits_no_commit_and_next_invocation_recovers(mcp, program):
    # At this size the reducer reaches the unchanged host memory ceiling even
    # before the transaction preflight. A host refusal is NOT a SIGIL -413.
    result = mcp.forge(program, input=snapshot(event(payload=submission("\x00" * 262144))), fuel=FUEL)
    assert result["status"] == "error"
    assert "guest allocation exceeds forge memory limit" in result["diagnostics"][0]["message"]
    assert not result.get("data", {}).get("output_text")
    assert plan(mcp, program, event()).decision.action == "model"


def test_create_only_intent_collision_rolls_back_the_state_write(mcp, program, native_store_binary, tmp_path):
    root = tmp_path / "records"
    grants = {"pi-a.state": "read_write", "pi-a.intent": "create_only", "pi-a.delivery": "read"}
    batch = plan(mcp, program, event())
    with NativeStore(native_store_binary, root, grants, initialize=True) as store:
        store.commit([mutation("pi-a.intent", "operation-1:1", "pre-existing")])
        assert store.raw(batch.raw)["code"] == "conflict"
        assert store.get("pi-a.state", "conversation") == {"revision": 0, "value": None}


def test_a_changed_delivery_revision_cannot_partially_advance_state_or_create_next_intent(
        mcp, program, native_store_binary, tmp_path):
    root = tmp_path / "records"
    app_grants = {"pi-a.state": "read_write", "pi-a.intent": "create_only", "pi-a.delivery": "read"}
    batch = plan(mcp, program, event())
    incoming = batch.decision.result(tool_reply("read_file"))
    delivered = observation(incoming)
    following = plan(mcp, program, incoming, 1, delivery=delivered)
    with NativeStore(native_store_binary, root, app_grants, initialize=True) as store:
        batch.commit(store)
    # Deliberately stronger fixture-only maintenance scope simulates changed
    # storage after a snapshot; production executor delivery scope is create-only.
    with NativeStore(native_store_binary, root, {"pi-a.delivery": "read_write"}) as writer:
        writer.commit([mutation("pi-a.delivery", "operation-1:1", delivered)])
        writer.commit([mutation("pi-a.delivery", "operation-1:1", delivered, 1)])
    with NativeStore(native_store_binary, root, app_grants) as store:
        assert store.raw(following.raw)["code"] == "conflict"
        assert store.get("pi-a.state", "conversation")["value"] == batch.decision.state
    with NativeStore(native_store_binary, root, {"pi-a.intent": "read"}) as reader:
        assert reader.get("pi-a.intent", "operation-1:2") == {"revision": 0, "value": None}


def test_two_scopes_with_identical_semantic_ids_remain_disjoint(mcp, program, native_store_binary, tmp_path):
    root = tmp_path / "records"
    for i, tenant in enumerate(("pi-a", "pi-b")):
        batch = plan(mcp, program, event(payload=submission(tenant)), prefix=tenant)
        grants = {f"{tenant}.state": "read_write", f"{tenant}.intent": "create_only", f"{tenant}.delivery": "read"}
        with NativeStore(native_store_binary, root, grants, initialize=i == 0) as store:
            batch.commit(store)
            assert fields(store.get(f"{tenant}.state", "conversation")["value"], "PT1\n", 17)[0] == "operation-1"
            other = "pi-b" if i == 0 else "pi-a"
            assert store.request({"op": "get", "namespace": f"{other}.state", "key": "conversation"})["code"] == "denied"
            forged = plan(mcp, program, event(), prefix=other)
            assert store.raw(forged.raw)["code"] == "denied"


def test_completed_conversation_followup_retains_history_after_restart_and_old_reply_cannot_overwrite_it(
        mcp, program, native_store_binary, tmp_path):
    root = tmp_path / "records"
    app = {"pi-a.state": "read_write", "pi-a.intent": "create_only", "pi-a.delivery": "read"}
    initial = plan(mcp, program, event())
    incoming = initial.decision.result(text_reply("The owner is Ada."))
    delivered = observation(incoming)
    with NativeStore(native_store_binary, root, app, initialize=True) as store:
        initial.commit(store)
    with NativeStore(native_store_binary, root, {"pi-a.delivery": "create_only"}) as executor:
        executor.commit([mutation("pi-a.delivery", "operation-1:1", delivered)])
    completed = plan(mcp, program, incoming, 1, delivery=delivered)
    with NativeStore(native_store_binary, root, app) as store:
        completed.commit(store)
        store.kill()
    with NativeStore(native_store_binary, root, app) as store:
        prior = store.get("pi-a.state", "conversation")
        next_event = event(prior=prior["value"], operation="operation-2",
                           payload=submission("Explain that.", key="submission-2"))
        following = plan(mcp, program, next_event, prior["revision"])
        following.commit(store)
        history = json.loads(following.decision.input)["messages"]
        assert history[1]["content"][0]["text"] == "The owner is Ada."
        assert history[2]["content"] == "Explain that."
        assert following.request["writes"][1]["key"] == "operation-2:1"
        assert store.raw(completed.raw)["code"] == "conflict"
        assert store.get("pi-a.state", "conversation")["value"] == following.decision.state


def test_transaction_builder_contains_no_host_or_alternate_reducer_policy():
    for path in ("app/pi/turn_transaction.sigil", "app/shared/store_protocol.sigil"):
        code = (PI_ROOT / path).read_text()
        stripped = "\n".join(line.split("//", 1)[0] for line in code.splitlines())
        assert 'extern "C"' not in stripped and "#[trusted]" not in stripped
        assert "AUTHORSHIP: hand-authored" in code
    assert "reduce_turn(" in (PI_ROOT / "app/pi/turn_transaction.sigil").read_text()
