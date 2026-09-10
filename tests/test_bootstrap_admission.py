"""Compiled AV2 registry checks and real entry/function chaining.

Direct envelopes are independent fixtures, not proof of native provenance.
The separate native tests prove refusal before application-state creation.
"""

import json

import pytest

from api_support import SCOPES, TOKEN_B, binding, credential
from conftest import PI_ROOT, SIGIL_ROOT, forge_ok, mcp as mcp, needs_toolchain
from readiness_admission_support import envelope, source, step
from scripts.compose_application import compose_application
from scripts.compose_bootstrap_admission import BOOT, MOVED, compose_bootstrap_admission, definition
from test_admission import profile
from test_readiness_admission import entry as entry
from test_request_entry import reply
from turn_support import FUEL, fields, record, refused


@pytest.fixture(scope="module")
def admission():
    needs_toolchain()
    return compose_bootstrap_admission(PI_ROOT, SIGIL_ROOT).text


def request(rows, version="AV2\n"):
    return record(version, [json.dumps([binding(row) for row in rows])])


def changed(row, field, value):
    facts = fields(row["facts"], "CF2\n", 15)
    facts[field] = value
    return {**row, "facts": record("CF2\n", facts)}


def test_both_artifacts_are_bounded_and_bind_the_new_protocol():
    built = compose_bootstrap_admission(PI_ROOT, SIGIL_ROOT)
    assert built == compose_bootstrap_admission(PI_ROOT, SIGIL_ROOT)
    assert len(built.text.encode()) <= 65536 and built.text.count("pub fn tool_main(") == 1
    assert {"app/pi/api.sigil", "app/pi/admission.sigil", "app/pi/bootstrap_validation.sigil",
            "scripts/compose_bootstrap_admission.py"} <= built.input_hashes.keys()
    assert '"AV2\\n"' in source().text and '"registry_validated"' in source().text
    assert "fn configured(" not in source().text and "fn same_tools(" not in source().text
    assert "fn configured(" in built.text and "fn same_tools(" in built.text
    for name, digest in {**MOVED, "boot": BOOT}.items():
        with pytest.raises(ValueError, match="changed"):
            definition(f"fn {name}() -> i64 {{ return 0; }}", name, digest)


@pytest.mark.parametrize("rows", [
    [credential()],
    [credential(), credential(TOKEN_B, principal="bob", tenant="tenant-b", prefix="b")],
    [credential(tools=["read_file", "list_files"]), credential("rotated",
        scopes=list(reversed(SCOPES)), tools=["list_files", "read_file"])],
    [credential(), credential("other", principal="bob", scopes=["ops:read"], tools=[])],
    [credential(profile=profile(turns=0))],
])
def test_real_entry_and_expanded_admission_complete_the_guarded_bootstrap(mcp, entry, admission, rows):
    body = json.dumps([binding(row) for row in rows])
    called = step(mcp, entry, stage="boot", body=body)
    assert called[:2] == ["call", "admission"] and called[3] == "registry"
    assert fields(called[2], "AV2\n", 1) == [body]
    result = forge_ok(mcp, admission, called[2], fuel=FUEL)
    assert result == "registry_validated"
    approved = step(mcp, entry, purpose="boot", stage="call", body=body,
                    continuation=called[3], observation=result, now=151)
    assert reply(approved) == (204, {}, None)
    assert fields(called[4], "TG1\n", 2) == ["150", "9000000000"]
    assert fields(approved[4], "TG1\n", 2) == ["151", "9000000000"]


@pytest.mark.parametrize("field,value", [(0, ""), (1, " tenant"), (2, "bad/epoch"),
    (3, "0100"), (4, "100"), (5, '["chat","chat"]'), (5, '["typo"]'),
    (6, '[1]'), (6, '["read_file","read_file"]'), (6, '["*","read_file"]'),
    (9, "0"), (9, "301"), (14, profile(per_turn="01")), (14, profile(config="invalid"))])
def test_av2_checks_every_row_authority_and_profile(mcp, admission, field, value):
    refused(mcp, admission, request([changed(credential(), field, value)]), 400)


@pytest.mark.parametrize("kind", ["missing", "extra", "read_only", "duplicate", "numeric", "bad_record"])
def test_av2_grants_must_be_exactly_the_six_scoped_read_write_namespaces(mcp, admission, kind):
    row = credential()
    grants = [record("CG1\n", [ns, access]) for ns, access in row["grants"].items()]
    if kind == "missing": grants.pop()
    if kind == "extra": grants.append(record("CG1\n", ["other", "read_write"]))
    if kind == "read_only": grants[0] = record("CG1\n", ["a.requests", "read"])
    if kind == "duplicate": grants[-1] = grants[0]
    if kind == "numeric": grants[-1] = 1
    if kind == "bad_record": grants[-1] = record("CG1\n", ["a.reservation", "read_write", "extra"])
    raw = record("CB1\n", [row["facts"], json.dumps(grants)])
    refused(mcp, admission, record("AV2\n", [json.dumps([raw])]), 400)


@pytest.mark.parametrize("coordinate", [7, 8, 10, 11, 12, 13])
@pytest.mark.parametrize("other", [7, 8, 10, 11, 12, 13])
def test_all_cross_tenant_namespace_pairs_are_disjoint(mcp, admission, coordinate, other):
    a = credential()
    b = credential(TOKEN_B, principal="bob", tenant="tenant-b", prefix="b")
    old = fields(b["facts"], "CF2\n", 15)[other]
    new = fields(a["facts"], "CF2\n", 15)[coordinate]
    b = changed(b, other, new)
    b["grants"] = {new if key == old else key: value for key, value in b["grants"].items()}
    refused(mcp, admission, request([a, b]), 400)


@pytest.mark.parametrize("field,value", [(2, "epoch-2"), (5, '["chat"]'),
    (6, '["list_files"]'), (9, "30"), (14, profile(turns=1))])
def test_same_principal_rotations_cannot_change_policy(mcp, admission, field, value):
    refused(mcp, admission, request([credential(), changed(credential("rotated"), field, value)]), 400)


def test_same_tenant_requires_identical_namespaces_and_budget_profile(mcp, admission):
    refused(mcp, admission, request([credential(), credential("other", principal="bob", prefix="b")]), 400)
    refused(mcp, admission, request([credential(), credential("other", principal="bob", profile=profile(turns=1))]), 400)


@pytest.mark.parametrize("raw", ["[]", "{}", "[1]", json.dumps([binding(credential())] * 65)])
def test_registry_is_a_bounded_nonempty_array_of_bindings(mcp, admission, raw):
    refused(mcp, admission, record("AV2\n", [raw]), 400)


def test_old_profile_only_function_cannot_satisfy_new_entry(mcp, entry, admission):
    rows = [credential(), credential(profile=profile(turns=0))]
    old = compose_application("admission", SIGIL_ROOT).text
    assert forge_ok(mcp, old, request(rows, "AV1\n"), fuel=FUEL) == "profiles_validated"
    assert forge_ok(mcp, admission, request(rows, "AV1\n"), fuel=FUEL) == "profiles_validated"
    refused(mcp, admission, request(rows), 400)
    refused(mcp, old, request([credential()]), 400)
    body = json.dumps([binding(credential())])
    for continuation, observed in [("profiles", "profiles_validated"), ("registry", "profiles_validated"),
                                   ("profiles", "registry_validated")]:
        refused(mcp, entry, envelope(stage="call", purpose="boot", body=body,
                continuation=continuation, observation=observed), 400)


def test_boot_guard_uses_only_currently_active_credentials_and_is_rechecked(mcp, entry, admission):
    rows = [credential(expires=175), credential(TOKEN_B, principal="bob", tenant="tenant-b", prefix="b", expires=160),
            credential("future", principal="future", tenant="future", prefix="c", before=200, expires=300)]
    body = json.dumps([binding(row) for row in rows])
    called = step(mcp, entry, now=150, stage="boot", body=body)
    assert fields(called[4], "TG1\n", 2) == ["150", "175"]
    observed = forge_ok(mcp, admission, called[2], fuel=FUEL)
    assert observed == "registry_validated"
    approved = step(mcp, entry, now=174, stage="call", purpose="boot", body=body,
                    continuation=called[3], observation=observed)
    assert fields(approved[4], "TG1\n", 2) == ["174", "175"]
    refused(mcp, entry, envelope(now=175, stage="call", purpose="boot", body=body,
            continuation=called[3], observation=observed), 400)
    refused(mcp, entry, envelope(now=99, stage="boot", body=body), 400)
