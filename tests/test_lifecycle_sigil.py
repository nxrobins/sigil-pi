"""Compiled SIGIL lifecycle decisions; native facts here are explicit fixtures."""

import json

import pytest

from conftest import mcp as mcp
from emergency_support import DOMAIN
from readiness_admission_support import durable_commit, envelope, step
from readset_support import observed as read_set
from test_readiness_admission import entry as entry, normal_plan
from test_request_entry import reply
from turn_support import fields, record, refused


@pytest.mark.parametrize("signal", [record("LF1\n", ["ok", "", "1"]),
    *[record("LF1\n", ["error", error, ""]) for error in
      ("signal_wrong_process", "signal_owner_changed", "signal_unavailable")]])
def test_new_operation_gate_follows_confirmed_accounting_and_dedup_lookup(mcp, entry, signal):
    commit = normal_plan(mcp, entry, ready=False)
    lookup = step(mcp, entry, stage="commit_observed", continuation=commit[3],
                  observation=durable_commit(), lifecycle=signal)
    assert lookup[0] == "read_many"  # A signal cannot hide an existing acceptance.
    rows = [(row["namespace"], row["key"], "0", "0", "") for row in json.loads(lookup[1])]
    denied = step(mcp, entry, stage="read_many", continuation=lookup[3],
                  observation=read_set(rows), lifecycle=signal)
    error = "service_draining" if fields(signal, "LF1\n", 3)[0] == "ok" else "lifecycle_unavailable"
    assert reply(denied)[::2] == (503, {"error": {"code": error}})
    assert fields(denied[4], "TG1\n", 2) == ["100", "9000000000"]


def test_running_observation_retains_the_original_new_operation_admission_call(mcp, entry):
    commit = normal_plan(mcp, entry, ready=False)
    lookup = step(mcp, entry, stage="commit_observed", continuation=commit[3], observation=durable_commit())
    rows = [(row["namespace"], row["key"], "0", "0", "") for row in json.loads(lookup[1])]
    admitted = step(mcp, entry, stage="read_many", continuation=lookup[3], observation=read_set(rows))
    assert admitted[:2] == ["call", "admission"]
    assert fields(admitted[2], "AP2\n", 11)[1] == fields(lookup[3], "RP5\n", 2)[0]


@pytest.mark.parametrize("signal", ["", record("LF2\n", ["ok", "", "0"]),
    record("LF1\n", ["ok", "", "00"]), record("LF1\n", ["ok", "", "2"]),
    record("LF1\n", ["ok", "signal_owner_changed", "0"]), record("LF1\n", ["ok", "", ""]),
    record("LF1\n", ["error", "signal_owner_changed", "0"]), record("LF1\n", ["error", "", ""]),
    record("LF1\n", ["error", "private-diagnostic", ""]), record("LF1\n", ["other", "", "0"])])
def test_malformed_lifecycle_facts_never_become_a_false_running_observation(mcp, entry, signal):
    refused(mcp, entry, envelope(lifecycle=signal), 400)


@pytest.mark.parametrize("process", ["", record("MC1\n", [DOMAIN, "1"]),
    record("PF1\n", [record("MC1\n", ["bad", "1"]), record("LF1\n", ["ok", "", "0"])]),
    record("PF1\n", [record("MC1\n", [DOMAIN, "01"]), record("LF1\n", ["ok", "", "0"])]),
    record("PF1\n", [record("MC1\n", [DOMAIN, "1"]), record("LF1\n", ["ok", "", "0"]), "extra"]),
    "x" * 513])
def test_opted_in_entry_rejects_legacy_or_malformed_process_container(mcp, entry, process):
    refused(mcp, entry, envelope(process=process), 400)


def test_unknown_credentials_do_not_learn_process_fact_errors(mcp, entry):
    denied = step(mcp, entry, row={"facts": ""}, process="invalid-host-fixture")
    assert reply(denied)[::2] == (401, {"error": {"code": "invalid_credential"}})


def test_uncommitted_or_malformed_body_is_not_reclassified_as_a_drain_rejection(mcp, entry):
    # No domain lookup or drain verdict is reached before durable admission and
    # the existing deferred malformed-body result.
    from conftest import forge_ok
    from request_policy_support import compose_request_policy, incoming
    from turn_support import FUEL
    proposed = forge_ok(mcp, compose_request_policy().text, incoming(body="{"), fuel=FUEL)
    commit = step(mcp, entry, body="{", stage="call", continuation="request-policy", observation=proposed)
    result = step(mcp, entry, body="{", stage="commit_observed", continuation=commit[3],
                  observation=durable_commit(), lifecycle=record("LF1\n", ["ok", "", "1"]))
    assert reply(result)[::2] == (400, {"error": {"code": "invalid_request"}})
