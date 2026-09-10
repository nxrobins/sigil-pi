"""Bounded native discovery; keys do not authorize dispatch or encode app policy."""
import pytest

from store_support import NativeStore, mutation


def page(store, namespace, after=None, limit=2):
    return store.request({"op": "keys", "namespace": namespace, "after": after, "limit": limit})


def test_actual_process_discovery_is_scoped_and_survives_restart(native_store_binary, tmp_path):
    root = tmp_path / "records"
    with NativeStore(native_store_binary, root, {"a": "read_write", "b": "read_write"}, initialize=True) as store:
        store.commit([mutation("a", "same", "private a"), mutation("b", "same", "private b"),
                      mutation("a", "next", "next value")])
        store.commit([mutation("a", "next", None, 1)])
        assert page(store, "a") == {"status": "ok", "page": {"keys": ["next", "same"], "next": "same"}}
        store.kill()
    with NativeStore(native_store_binary, root, {"a": "read"}) as store:
        assert page(store, "b") == {"status": "error", "code": "denied"}
        assert page(store, "a", "same")["page"] == {"keys": [], "next": None}
        assert page(store, "a")["page"]["keys"] == ["next", "same"]
        assert store.get("a", "next") == {"revision": 2, "value": None}
        assert store.get("a", "same")["revision"] == 1


def test_create_only_process_cannot_discover_names(native_store_binary, tmp_path):
    with NativeStore(native_store_binary, tmp_path / "records", {"a": "create_only"}, initialize=True) as store:
        store.commit([mutation("a", "same", "private value")])
        assert page(store, "a") == {"status": "error", "code": "denied"}


@pytest.mark.parametrize("field,value", [("limit", 0), ("limit", 129), ("limit", True),
    ("limit", "1"), ("limit", 1.5), ("after", ""), ("after", "x" * 257),
    ("after", "bad/key"), ("after", {"scope": "*"}), ("namespace", "bad/name"),
    ("grants", ["*"]), ("sql", "SELECT * FROM records")])
def test_discovery_requests_cannot_widen_authority_or_remove_bounds(native_store_binary, tmp_path, field, value):
    with NativeStore(native_store_binary, tmp_path / "records", {"a": "read_write"}, initialize=True) as store:
        store.commit([mutation("a", "one", "retained")])
        request = {"op": "keys", "namespace": "a", "after": None, "limit": 1, field: value}
        assert store.request(request) == {"status": "error", "code": "invalid"}
        assert store.get("a", "one") == {"revision": 1, "value": "retained"}
