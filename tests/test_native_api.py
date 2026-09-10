"""SIGIL policy plus actual native HTTP/fact/storage binding, not a mock API."""
import concurrent.futures
import copy
import json
import socket

import pytest

from api_support import BODY, SCOPES, TOKEN_A, TOKEN_B, NativeApi, binding, credential, envelope
from conftest import PI_ROOT, SIGIL_ROOT, forge_ok, needs_toolchain
from scripts.compose_application import compose_application
from store_support import NativeStore
from turn_support import FUEL, fields, record
from test_admission import profile


@pytest.fixture(scope="module")
def api_program():
    needs_toolchain()
    return compose_application("api", SIGIL_ROOT).text


def decision(mcp, program, incoming):
    return fields(forge_ok(mcp, program, incoming, fuel=FUEL), "HC3\n", 5)


@pytest.fixture(scope="module")
def admission_program():
    needs_toolchain()
    return compose_application("admission", SIGIL_ROOT).text


def admission_command(mcp, api_program, admission_program, *, row=None, now=150):
    missing = record("SR1\n", ["ok", "0", ""])
    next_command = decision(mcp, api_program, envelope(row=row, now=now, stage="read", observation=missing, continuation="dedup"))
    assert next_command[:2] == ["read", "a.state"]
    next_command = decision(mcp, api_program, envelope(row=row, now=now, stage="read", observation=missing, continuation=next_command[3]))
    assert next_command[:3] == ["read", "a.budget", "active"]
    next_command = decision(mcp, api_program, envelope(row=row, now=now, stage="read", observation=missing, continuation=next_command[3]))
    assert next_command[:2] == ["call", "admission"]
    planned = forge_ok(mcp, admission_program, next_command[2], fuel=FUEL)
    return decision(mcp, api_program, envelope(row=row, now=now, stage="call", observation=planned, continuation=next_command[3]))


def test_sigil_bootstrap_and_first_authorized_read(mcp, api_program):
    boot = envelope(stage="boot", body=json.dumps([binding(credential())]))
    called = decision(mcp, api_program, boot)
    assert called[:2] == ["call", "admission"]
    assert fields(called[2], "AV1\n", 1) == [json.dumps([binding(credential())])]
    approved = envelope(stage="call", purpose="boot", body=json.dumps([binding(credential())]), observation="profiles_validated", continuation="profiles")
    assert decision(mcp, api_program, approved) == ["reply", "204", "", "", record("TG1\n", ["150", "9000000000"])]
    assert decision(mcp, api_program, envelope()) == ["read", "a.requests", "616c696365:same-key", "dedup",
                                                   record("TG1\n", ["100", "9000000000"])]


@pytest.mark.parametrize("now,status", [(99, 401), (100, 200), (199, 200), (200, 401)])
def test_sigil_owns_credential_time_boundary(mcp, api_program, now, status):
    result = decision(mcp, api_program, envelope(row=credential(expires=200), now=now))
    assert (result[0] == "read") if status == 200 else (result[:2] == ["reply", "401"])
    assert result[4] == (record("TG1\n", ["100", "200"]) if status == 200 else "")


@pytest.mark.parametrize("scope", [[], ["sessions:read"], ["ops:read"]])
def test_sigil_denies_insufficient_permission_before_read(mcp, api_program, scope):
    assert decision(mcp, api_program, envelope(row=credential(scopes=scope)))[:2] == ["reply", "403"]


def test_equivalent_rotated_credentials_keep_set_policy_and_key_identity(mcp, api_program):
    a = credential(tools=["read_file", "list_files"])
    b = credential("rotated", scopes=list(reversed(SCOPES)), tools=["list_files", "read_file"])
    assert decision(mcp, api_program, envelope(stage="boot", body=json.dumps([binding(a), binding(b)])))[:2] == ["call", "admission"]
    assert decision(mcp, api_program, envelope(row=a)) == decision(mcp, api_program, envelope(row=b))


def test_sigil_tombstone_blocks_reexecution_and_failed_commit_is_not_accepted(mcp, api_program):
    tombstone = record("SR1\n", ["ok", "2", ""])
    assert decision(mcp, api_program, envelope(stage="read", observation=tombstone, continuation="dedup"))[:2] == ["reply", "409"]
    failed = record("SC1\n", ["error", "0"])
    assert decision(mcp, api_program, envelope(stage="commit", observation=failed, continuation="a" * 64))[:2] == ["reply", "503"]


def test_sigil_operation_deadline_cannot_extend_credential_expiry(mcp, api_program, admission_program):
    command = admission_command(mcp, api_program, admission_program, row=credential(expires=175), now=150)
    assert command[0] == "commit"
    writes = json.loads(command[1])["writes"]
    assert fields(writes[1]["value"], "OQ2\n", 11)[6:8] == ["150", "175"]
    assert fields(command[4], "TG1\n", 2) == ["100", "175"]


def test_sigil_commit_guard_uses_the_tighter_turn_deadline(mcp, api_program, admission_program):
    command = admission_command(mcp, api_program, admission_program, row=credential(turn_seconds=1), now=150)
    assert command[0] == "commit"
    assert fields(command[4], "TG1\n", 2) == ["100", "151"]
    assert fields(json.loads(command[1])["writes"][1]["value"], "OQ2\n", 11)[7] == "151"


@pytest.mark.parametrize("stage,observation,continuation,status", [
    ("init", "", "", "403"),
    ("commit", record("SC1\n", ["ok", "1"]), "a" * 64, "202"),
    ("commit", record("SC1\n", ["error", "0"]), "a" * 64, "503"),
])
def test_sigil_authenticated_replies_keep_the_credential_guard(mcp, api_program, stage, observation, continuation, status):
    row = credential(scopes=[] if status == "403" else None, expires=200)
    command = decision(mcp, api_program, envelope(row=row, stage=stage, observation=observation, continuation=continuation))
    assert command[:2] == ["reply", status]
    assert fields(command[4], "TG1\n", 2) == ["100", "200"]


def test_sigil_rejects_the_unguarded_envelope_version(mcp, api_program):
    result = mcp.forge(api_program, input=envelope().replace("AH3\n", "AH1\n", 1), fuel=FUEL)
    assert result["status"] == "error", result
    diagnostic = result["diagnostics"][0]
    assert diagnostic["code"] == "R803", diagnostic
    assert diagnostic["message"] == "tool trapped: tool returned error (400)", diagnostic


@pytest.fixture(scope="module")
def guard_probe_program(api_program):
    # Owner-installed, solver-verified HOST CONFORMANCE PROBE, not the product API.
    # It deliberately returns arbitrary command bytes to challenge the native
    # ceiling. An HTTP caller cannot replace the production pinned source this way.
    assert api_program.count("pub fn tool_main(") == 1
    return api_program.replace("pub fn tool_main(", "fn original_main(") + """
pub fn tool_main(input_ptr: i64, input_len: i64) -> i64 @Internal ! { Alloc } {
    let original: i64 @Internal = original_main(input_ptr, input_len);
    let x: i64 @Internal = read_record(slice(input_ptr, input_len), "AH3\\n", 12);
    if x < 0 { return x; }
    if is_text(get(x, 11), "boot") { return original; }
    if is_text(get(x, 6), "commit") {
        return command("reply", text("200"), text("{}"), text(""));
    }
    return get(x, 2);
}
"""


@pytest.mark.parametrize("kind", ["missing", "expired", "future", "malformed", "trailing", "legacy", "expired_read", "expired_reply", "valid"])
def test_native_host_checks_time_guards_from_real_verified_workers(native_service_binary, native_store_binary, tmp_path, guard_probe_program, kind):
    guard = record("TG1\n", ["100", "9000000000"])
    if kind == "missing":
        guard = ""
    if kind.startswith("expired"):
        guard = record("TG1\n", ["1", "2"])
    if kind == "future":
        guard = record("TG1\n", ["9000000000", "9000000001"])
    if kind == "malformed":
        guard = record("TG1\n", ["0100", "9000000000"])
    if kind == "trailing":
        guard += "forged"
    batch = {"op": "commit", "checks": [], "writes": [
        {"namespace": "a.operations", "key": "probe", "revision": 0, "value": "native-guard-probe"}]}
    values = ["commit", json.dumps(batch), "", "held", guard]
    if kind == "expired_read":
        values[:4] = ["read", "a.operations", "probe", "held"]
    if kind == "expired_reply":
        values[:4] = ["reply", "200", '{"protected":"must-not-escape"}', ""]
    raw = record("HC1\n", values[:4]) if kind == "legacy" else record("HC3\n", values)
    root = tmp_path / "probe"
    with NativeApi(native_service_binary, root, source=guard_probe_program) as api:
        status, body = api.request(raw=raw.encode())
        assert status == (200 if kind == "valid" else 503), (kind, status, body)
        assert "must-not-escape" not in json.dumps(body)
    with NativeStore(native_store_binary, root / "records", {"a.operations": "read"}) as store:
        retained = store.get("a.operations", "probe")
        assert retained["revision"] == (1 if kind == "valid" else 0), retained
        assert retained["value"] == ("native-guard-probe" if kind == "valid" else None), retained


def test_legacy_host_configuration_cannot_open_product_state(native_service_binary, tmp_path):
    with NativeApi(native_service_binary, tmp_path / "baseline") as api:
        config = api.config
    root = tmp_path / "legacy"
    config["version"] = 1
    config["state_root"] = str(root / "records")
    with pytest.raises(AssertionError, match='"code":"config"'):
        NativeApi(native_service_binary, root, config=config)
    assert not (root / "records").exists()


def test_legacy_sigil_bootstrap_response_cannot_open_product_state(native_service_binary, tmp_path, guard_probe_program):
    old_boot = guard_probe_program.replace(
        'return original;',
        'let old: i64 @Internal = read_record(original, "HC3\\n", 5); return write_record(old, "HC1\\n", 4);')
    assert old_boot != guard_probe_program
    root = tmp_path / "legacy"
    with pytest.raises(AssertionError, match='"code":"protocol"'):
        NativeApi(native_service_binary, root, source=old_boot)
    assert not (root / "records").exists()


@pytest.mark.parametrize("guard,error", [("", "protocol"), (record("TG1\n", ["1", "2"]), "time_guard")])
def test_missing_or_expired_bootstrap_guard_never_opens_state(native_service_binary, tmp_path, guard_probe_program, guard, error):
    response = record("HC3\n", ["reply", "204", "", "", guard])
    source = guard_probe_program.replace('return original;',
                                        'return text(' + json.dumps(response) + ');')
    assert source != guard_probe_program
    root = tmp_path / "invalid-boot"
    with pytest.raises(AssertionError, match='"code":"' + error + '"'):
        NativeApi(native_service_binary, root, source=source)
    assert not (root / "records").exists()


def test_sigil_bootstrap_guard_ends_when_last_currently_active_credential_expires(mcp, api_program):
    rows = [credential(expires=175), credential(TOKEN_B, principal="bob", tenant="tenant-b", prefix="b", expires=160),
            credential("future", principal="future", tenant="future", prefix="c", before=200, expires=300)]
    command = decision(mcp, api_program, envelope(now=150, stage="boot", body=json.dumps([binding(row) for row in rows])))
    assert command[:2] == ["call", "admission"]
    assert fields(command[4], "TG1\n", 2) == ["150", "175"]


def test_native_http_commits_replays_and_restores_the_same_operation(native_service_binary, native_store_binary, tmp_path):
    needs_toolchain()
    root = tmp_path / "service"
    with NativeApi(native_service_binary, root) as api:
        status, first = api.request()
        assert status == 202 and first["status"] == "accepted" and not first["replayed"]
        replay_body = json.dumps(dict(reversed(list(BODY.items()))), indent=2).encode()
        status, duplicate = api.request(raw=replay_body)
        assert status == 202 and duplicate["replayed"] and duplicate["operation"] == first["operation"]
        assert api.request(body={**BODY, "message": "changed"})[0] == 409
        assert api.request("GET", first["status_url"])[1]["operation"] == first["operation"]
    with NativeStore(native_store_binary, root / "records", dict.fromkeys(credential()["grants"], "read")) as store:
        retained = store.get("a.operations", first["operation"])
        values = fields(retained["value"], "OQ2\n", 11)
        assert values[:3] == ["alice", "tenant-a", "same-key"] and values[9] == "accepted"
        assert fields(values[3], "PS1\n", 3) == [BODY["session"], BODY["message"], BODY["submission_key"]]
        assert int(values[7]) - int(values[6]) == 120
        dedup = store.get("a.requests", "616c696365:same-key")
        assert dedup["revision"] == retained["revision"]  # same atomic batch
        assert TOKEN_A not in retained["value"] and TOKEN_A not in dedup["value"]
        snapshots = [store.get("a.state", BODY["session"]), store.get("a.intent", first["operation"] + ":1"),
                     store.get("a.budget", "active"), store.get("a.reservation", first["operation"])]
        assert all(r["revision"] == 1 for r in snapshots)  # all newly created in this one admission
        assert fields(snapshots[0]["value"], "PT1\n", 17)[:3] == [first["operation"], "model", "1"]
        intent = fields(snapshots[1]["value"], "SI1\n", 5)
        assert intent[:4] == [first["operation"], "1", "model", ""]
        assert [spec["name"] for spec in json.loads(intent[4])["tools"]] == ["read_file"]
        assert fields(snapshots[2]["value"], "BH1\n", 5)[2:] == ["1", "20000", "4096"]
        reservation = fields(snapshots[3]["value"], "BR1\n", 9)
        assert reservation[0] == first["operation"] and reservation[7] == "reserved"
        assert reservation[8] == values[10] and len(values[10]) == 64
        assert all(TOKEN_A not in r["value"] for r in snapshots)
    with NativeApi(native_service_binary, root, mode="open") as api:
        assert api.request()[1]["operation"] == first["operation"]
        assert api.request("GET", first["status_url"])[0] == 200


def test_native_service_binds_two_tenants_and_two_same_tenant_principals(native_service_binary, tmp_path):
    rows = [credential(), credential(TOKEN_B, principal="bob", tenant="tenant-b", prefix="b"),
            credential("peer-token", principal="peer")]
    with NativeApi(native_service_binary, tmp_path / "service", credentials=rows) as api:
        _, a = api.request()
        _, b = api.request(token=TOKEN_B)
        _, peer = api.request(token="peer-token", body={**BODY, "session": "peer-session"})
        assert len({a["operation"], b["operation"], peer["operation"]}) == 3
        assert api.request("GET", a["status_url"], token=TOKEN_B)[0] == 404
        assert api.request("GET", a["status_url"], token="peer-token")[0] == 404
        assert api.request("GET", b["status_url"])[0] == 404


def test_concurrent_duplicates_converge_on_one_committed_identity(native_service_binary, tmp_path):
    with NativeApi(native_service_binary, tmp_path / "service") as api:
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            replies = list(pool.map(lambda _: api.request(), range(4)))
        assert all(status == 202 for status, _ in replies), replies
        assert len({v["operation"] for _, v in replies}) == 1
        assert sum(not v["replayed"] for _, v in replies) == 1


def test_capacity_is_tenant_scoped_durable_and_replay_does_not_reserve_twice(native_service_binary, native_store_binary, tmp_path):
    policy = profile(turns=1, inputs=20000, outputs=4096)
    rows = [credential(profile=policy), credential(TOKEN_B, principal="bob", tenant="tenant-b", prefix="b", profile=policy)]
    root = tmp_path / "capacity"
    extra = {**BODY, "session": "second", "submission_key": "second"}
    with NativeApi(native_service_binary, root, credentials=rows) as api:
        status, first = api.request()
        assert status == 202
        assert api.request(body=extra) == (429, {"error": {"code": "quota_exceeded"}})
        assert api.request(token=TOKEN_B)[0] == 202
        assert api.request()[1]["operation"] == first["operation"]
    with NativeApi(native_service_binary, root, credentials=rows, mode="open") as api:
        assert api.request(body=extra)[0] == 429
        assert api.request()[1]["replayed"]
    grants = {name: "read" for row in rows for name in row["grants"]}
    with NativeStore(native_store_binary, root / "records", grants) as store:
        for prefix in ["a", "b"]:
            held = store.get(prefix + ".budget", "active")
            assert held["revision"] == 1
            assert fields(held["value"], "BH1\n", 5)[2:] == ["1", "20000", "4096"]
        assert store.get("a.requests", "616c696365:second")["revision"] == 0
        assert store.get("a.state", "second")["revision"] == 0


def test_concurrent_distinct_submissions_cannot_overwrite_one_active_conversation(native_service_binary, native_store_binary, tmp_path):
    root = tmp_path / "concurrent"
    bodies = [{**BODY, "submission_key": "first"}, {**BODY, "submission_key": "second"}]
    with NativeApi(native_service_binary, root) as api:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            replies = list(pool.map(lambda body: api.request(body=body), bodies))
        assert sorted(status for status, _ in replies) == [202, 409], replies
        rejected_index = next(i for i, (status, _) in enumerate(replies) if status == 409)
        assert replies[rejected_index][1] == {"error": {"code": "session_busy"}}
    with NativeStore(native_store_binary, root / "records", dict.fromkeys(credential()["grants"], "read")) as store:
        assert fields(store.get("a.budget", "active")["value"], "BH1\n", 5)[2:] == ["1", "20000", "4096"]
        assert store.get("a.requests", "616c696365:" + bodies[rejected_index]["submission_key"])["revision"] == 0


@pytest.mark.parametrize("policy", [profile(config=""), profile(per_turn=0), profile(turns="01")])
def test_invalid_admission_profile_is_refused_before_state_initialization(native_service_binary, tmp_path, policy):
    root = tmp_path / "bad-profile"
    with pytest.raises(AssertionError, match='"code":"application"'):
        NativeApi(native_service_binary, root, credentials=[credential(profile=policy)])
    assert not (root / "records").exists()


@pytest.fixture(scope="module")
def native_configuration(native_service_binary, tmp_path_factory):
    with NativeApi(native_service_binary, tmp_path_factory.mktemp("native-config")) as api:
        return api.config


@pytest.mark.parametrize("function_name", ["admission", "history"])
@pytest.mark.parametrize("change", ["network", "filesystem", "secret", "source_hash", "runtime_hash", "extra", "missing", "missing_one", "alias", "too_many"])
def test_function_registration_cannot_widen_authority_or_admit_unapproved_code(native_service_binary, native_configuration, tmp_path, change, function_name):
    config = copy.deepcopy(native_configuration)
    root = tmp_path / "bad-function"
    config["state_root"] = str(root / "records")
    function = config["functions"][function_name]
    if change == "network": function["net"] = ["*"]
    if change == "filesystem": function["fs"] = ["/tmp"]
    if change == "secret": function["secret_env"] = {"provider": "UNUSED_FIXTURE_SECRET"}
    if change == "source_hash": function["source_sha256"] = "0" * 64
    if change == "runtime_hash": function["runtime_sha256"] = "0" * 64
    if change == "extra": config["functions"]["unexpected"] = function
    if change == "missing": config["functions"] = {}
    if change == "missing_one": del config["functions"][function_name]
    if change == "alias": config["functions"] = {"bad/name": function}
    if change == "too_many": config["functions"] = {f"extra{i}": function for i in range(9)}
    with pytest.raises(AssertionError):
        NativeApi(native_service_binary, root, config=config)
    assert not (root / "records").exists()


def test_pure_function_result_is_data_not_a_native_command(native_service_binary, native_store_binary, tmp_path, admission_program):
    malicious = record("HC3\n", ["commit", json.dumps({"op": "commit", "checks": [], "writes": [
        {"namespace": "a.operations", "key": "injected", "revision": 0, "value": "must-not-commit"}]}),
        "", "", record("TG1\n", ["100", "9000000000"])])
    source = admission_program.replace("pub fn tool_main(", "fn original_main(") + """
pub fn tool_main(input_ptr: i64, input_len: i64) -> i64 @Internal ! { Alloc } {
    let original: i64 @Internal = original_main(input_ptr, input_len);
    if input_len >= 4 && is_text(slice(input_ptr, 4), "AV1\\n") { return original; }
    return text(""" + json.dumps(malicious) + """);
}
"""
    root = tmp_path / "function-result"
    with NativeApi(native_service_binary, root, function_source=source) as api:
        assert api.request()[0] == 503
    with NativeStore(native_store_binary, root / "records", dict.fromkeys(credential()["grants"], "read")) as store:
        assert store.get("a.operations", "injected")["revision"] == 0
        assert store.get("a.requests", "616c696365:same-key")["revision"] == 0
        assert store.get("a.budget", "active")["revision"] == 0


def test_entry_cannot_call_an_unregistered_function(native_service_binary, native_store_binary, tmp_path, guard_probe_program):
    root = tmp_path / "unknown-function"
    command = record("HC3\n", ["call", "unregistered", "payload", "held", record("TG1\n", ["100", "9000000000"])])
    with NativeApi(native_service_binary, root, source=guard_probe_program) as api:
        assert api.request(raw=command.encode())[0] == 503
    with NativeStore(native_store_binary, root / "records", {"a.budget": "read"}) as store:
        assert store.get("a.budget", "active")["revision"] == 0


def test_native_capacity_failure_rolls_back_the_whole_public_admission(native_service_binary, native_store_binary, native_configuration, tmp_path):
    config = copy.deepcopy(native_configuration)
    root = tmp_path / "store-capacity"
    config["state_root"] = str(root / "records")
    config["limits"]["records"] = 5  # admission requires six records; capacity checks run inside the transaction
    with NativeApi(native_service_binary, root, config=config) as api:
        assert api.request() == (503, {"error": {"code": "storage_unavailable"}})
    read_config = {"version": 1, "limits": config["limits"],
                   "grants": [{"namespace": ns, "access": "read"} for ns in credential()["grants"]]}
    with NativeStore(native_store_binary, root / "records", {}, config=read_config) as store:
        for ns, key in [("a.requests", "616c696365:same-key"), ("a.state", BODY["session"]), ("a.budget", "active")]:
            assert store.get(ns, key)["revision"] == 0


def test_same_tenant_principals_cannot_split_the_budget_policy(native_service_binary, tmp_path):
    root = tmp_path / "split-policy"
    rows = [credential(), credential("peer", principal="peer", profile=profile(turns=1))]
    with pytest.raises(AssertionError, match='"code":"application"'):
        NativeApi(native_service_binary, root, credentials=rows)
    assert not (root / "records").exists()


@pytest.mark.parametrize("field", ["tenant", "principal", "facts", "grants", "stage", "observation", "continuation", "operation",
                                   "function", "bundle", "purpose", "budget", "profile"])
def test_http_body_cannot_select_trusted_context(native_service_binary, tmp_path, field):
    with NativeApi(native_service_binary, tmp_path / "service") as api:
        assert api.request(body={**BODY, field: "forged"})[0] == 400
        status, legitimate = api.request()
        assert status == 202 and not legitimate["replayed"]


@pytest.mark.parametrize("raw", [b'{}', b'[]', b'null', b'{', b'\xff',
    json.dumps(BODY).encode()[:-1] + b',"session":"duplicate"}',
    json.dumps(BODY).encode()[:-1] + b',"s\\u0065ssion":"duplicate"}'])
def test_http_malformed_body_cannot_commit(native_service_binary, tmp_path, raw):
    with NativeApi(native_service_binary, tmp_path / "service") as api:
        assert api.request(raw=raw)[0] == 400
        assert not api.request()[1]["replayed"]


@pytest.mark.parametrize("kind", ["missing", "unknown", "insufficient"])
def test_all_inventory_routes_enforce_sigil_authorization(native_service_binary, tmp_path, kind):
    matrix = json.loads((PI_ROOT / "config/api-migration.json").read_text())
    row = credential(scopes=[]) if kind == "insufficient" else credential()
    token = {"missing": None, "unknown": "unknown", "insufficient": TOKEN_A}[kind]
    with NativeApi(native_service_binary, tmp_path / "service", credentials=[row]) as api:
        for route in matrix["legacy_routes"] + matrix["planned_routes"]:
            path = route.get("sample_path", route["path"].replace("{operation}", "a" * 64).replace("{session}", "session"))
            status, _ = api.request(route["method"], path, token=token)
            assert status == (403 if kind == "insufficient" else 401), (route, status)


@pytest.mark.parametrize("extra", [
    b"Authorization: Bearer second\r\n",
    b"Transfer-Encoding: chunked\r\n",
    b"Content-Length: 1\r\n",
])
def test_ambiguous_transport_is_refused_before_application_commit(native_service_binary, tmp_path, extra):
    with NativeApi(native_service_binary, tmp_path / "service") as api:
        raw = (b"POST /v1/operations HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer " + TOKEN_A.encode()
               + b"\r\nContent-Length: 0\r\n" + extra + b"\r\n0\r\n\r\n")
        with socket.create_connection(("127.0.0.1", api.port), timeout=5) as conn:
            conn.sendall(raw)
            reply = conn.recv(4096)
            assert reply.startswith(b"HTTP/1.1 400"), reply
        assert not api.request()[1]["replayed"]


@pytest.mark.parametrize("change", ["duplicate_digest", "cross_tenant_namespace", "extra_scope", "guest_network", "scope_typo", "numeric_tool"])
def test_unsafe_bootstrap_never_opens_product_state(native_service_binary, tmp_path, change):
    rows = [credential()]
    if change == "duplicate_digest":
        rows.append(credential())
    if change == "cross_tenant_namespace":
        rows.append(credential(TOKEN_B, principal="bob", tenant="tenant-b", prefix="a"))
    if change == "extra_scope":
        rows[0]["grants"]["b.operations"] = "read_write"
    if change == "scope_typo":
        rows[0] = credential(scopes=["administrator"])
    if change == "numeric_tool":
        rows[0] = credential(tools=[42])
    root = tmp_path / "service"
    if change == "guest_network":
        # Build a valid config without starting a lasting server, then mutate only
        # the independent host ceiling and demand startup refusal on fresh state.
        with NativeApi(native_service_binary, tmp_path / "baseline") as baseline:
            config = baseline.config
        config["state_root"] = str(root / "records")
        config["worker"]["net"] = ["*"]
        with pytest.raises(AssertionError):
            NativeApi(native_service_binary, root, config=config)
    else:
        with pytest.raises(AssertionError):
            NativeApi(native_service_binary, root, credentials=rows)
    assert not (root / "records").exists()
