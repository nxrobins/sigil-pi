"""Native durability/stdio conformance; not an authenticated product service."""

import json

import pytest

from store_support import LIMITS, NativeStore, mutation


def test_native_build_and_process_restart_preserve_an_atomic_batch(native_store_binary, tmp_path):
    root = tmp_path / "native-state"
    with NativeStore(native_store_binary, root, {"state": "read_write", "intent": "create_only"},
                     initialize=True) as store:
        assert tuple(map(int, store.ready["sqlite"].split("."))) >= (3, 51, 3)
        store.commit([mutation("state", "same", "Σ\x00state"), mutation("intent", "op:1", "opaque")])
        store.kill()
    with NativeStore(native_store_binary, root, {"state": "read", "intent": "read"}) as store:
        assert store.get("state", "same") == {"revision": 1, "value": "Σ\x00state"}
        assert store.get("intent", "op:1") == {"revision": 1, "value": "opaque"}


@pytest.mark.parametrize("payload", [
    '{"op":"get","namespace":"a","key":"x","scope":"all"}',
    '{"op":"get","namespace":"a","namespace":"b","key":"x"}',
    '{"op":"get","namespace":7,"key":"x"}',
    '{"op":"get","namespace":"a","key":"x"} {}',
    '{"op":"commit","writes":[{"namespace":"a","key":"x","revision":0}],"checks":[]}',
    '{"op":"commit","writes":[{"namespace":"a","key":"x","revision":0,"value":"x","grants":[]}],"checks":[]}',
    '{"op":"commit","writes":[],"checks":[],"sql":"DROP TABLE records"}',
    '{"op":"commit","writes":[],"checks":[]}',
    '{"op":"grant","namespace":"b"}',
    "null", "[]", "invalid",
])
def test_stdio_rejects_ambiguous_shapes_and_authority_fields(native_store_binary, tmp_path, payload):
    with NativeStore(native_store_binary, tmp_path / "state", {"a": "read_write"}, initialize=True) as store:
        assert store.raw(payload) == {"status": "error", "code": "invalid"}
        assert store.get("a", "x") == {"revision": 0, "value": None}


@pytest.mark.parametrize("revision", [-1, 1.0, "0", True, 9223372036854775808])
def test_revision_numbers_are_strict_and_bounded(native_store_binary, tmp_path, revision):
    with NativeStore(native_store_binary, tmp_path / "state", {"a": "read_write"}, initialize=True) as store:
        payload = {"op": "commit", "checks": [], "writes": [mutation("a", "x", "no", revision)]}
        assert store.request(payload) == {"status": "error", "code": "invalid"}


def test_process_scope_cannot_be_changed_by_a_request(native_store_binary, tmp_path):
    root = tmp_path / "state"
    with NativeStore(native_store_binary, root, {"app": "read_write"}, initialize=True) as store:
        store.commit([mutation("app", "same", "domain-private")])
    with NativeStore(native_store_binary, root, {"intent": "read", "delivery": "create_only"}) as store:
        assert store.request({"op": "get", "namespace": "app", "key": "same"}) == {
            "status": "error", "code": "denied"}
        store.commit([mutation("delivery", "op:1", "observed")])
        assert store.request({"op": "commit", "checks": [], "writes": [mutation("app", "same", "bad", 1)]}) == {
            "status": "error", "code": "denied"}


def test_bootstrap_duplicate_scope_is_refused_before_database_creation(native_store_binary, tmp_path):
    root = tmp_path / "state"
    config = {"version": 1, "limits": LIMITS,
              "grants": [{"namespace": "a", "access": "read"},
                         {"namespace": "a", "access": "read_write"}]}
    with pytest.raises(AssertionError, match='"code":"invalid"'):
        NativeStore(native_store_binary, root, {}, initialize=True, config=config)
    assert not (root / "records.sqlite").exists()


def test_explicit_null_is_a_versioned_tombstone_not_a_missing_field(native_store_binary, tmp_path):
    with NativeStore(native_store_binary, tmp_path / "state", {"a": "read_write"}, initialize=True) as store:
        store.commit([mutation("a", "x", "data")])
        store.commit([mutation("a", "x", None, 1)])
        assert store.get("a", "x") == {"revision": 2, "value": None}
        response = store.request({"op": "commit", "checks": [], "writes": [mutation("a", "x", "replay")]})
        assert response == {"status": "error", "code": "conflict"}
        assert "data" not in json.dumps(response)
