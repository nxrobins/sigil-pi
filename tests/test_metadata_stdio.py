"""Actual versioned store executable; scoped mechanisms, not public API policy."""
import json
import subprocess

from store_support import LIMITS, NativeStore, mutation


def run(binary, root, version, requests, *, access="read_write"):
    root.mkdir(mode=0o700)
    config = root.parent / (root.name + ".json")
    config.write_text(json.dumps({"version": version, "limits": LIMITS,
        "grants": [{"namespace": "app", "access": access}]}))
    result = subprocess.run([str(binary), "init", str(root), str(config)],
        input="".join(json.dumps(request) + "\n" for request in requests),
        capture_output=True, text=True, timeout=10)
    return result, [json.loads(line) for line in result.stdout.splitlines()]


def test_actual_store_version_two_advertises_and_returns_only_metadata(
        discovery_store_binary, legacy_native_store_binary, tmp_path):
    result, seen = run(discovery_store_binary, tmp_path / "v2", 2, [
        {"op": "commit", "checks": [], "writes": [mutation("app", "a", "private-canary-é")]},
        {"op": "metadata", "namespace": "app", "after": None, "limit": 1},
        {"op": "metadata", "namespace": "app", "after": "a", "limit": 1},
        {"op": "metadata", "namespace": "foreign", "after": None, "limit": 1},
        {"op": "get", "namespace": "app", "key": "a"},
    ])
    assert result.returncode == 0 and not result.stderr
    assert seen[0]["protocol"] == "sigil-store/v2"
    assert seen[1]["receipt"]["revision"] == 1
    assert seen[2] == {"status": "ok", "page": {"entries": [{"key": "a", "revision": "1",
        "present": True, "value_bytes": len("private-canary-é".encode())}], "next": "a"}}
    assert seen[3] == {"status": "ok", "page": {"entries": [], "next": None}}
    assert seen[4]["status"] == "error"
    assert "canary" not in json.dumps(seen[2:5])
    assert seen[5]["record"] == {"revision": 1, "value": "private-canary-é"}
    # New transport support does not change the on-disk schema or old get path.
    with NativeStore(legacy_native_store_binary, tmp_path / "v2", {"app": "read"}) as old:
        assert old.get("app", "a") == seen[5]["record"]


def test_actual_store_create_only_cannot_enumerate_metadata(discovery_store_binary, tmp_path):
    result, seen = run(discovery_store_binary, tmp_path / "creator", 2,
        [{"op": "metadata", "namespace": "app", "limit": 1}], access="create_only")
    assert result.returncode == 0 and seen[1]["status"] == "error"


def test_legacy_profile_does_not_gain_metadata_and_older_binary_refuses_v2(
        discovery_store_binary, legacy_native_store_binary, tmp_path):
    for label, binary in [("new", discovery_store_binary), ("old", legacy_native_store_binary)]:
        result, seen = run(binary, tmp_path / label, 1, [
            {"op": "metadata", "namespace": "app", "limit": 1},
            {"op": "keys", "namespace": "app", "limit": 1},
        ])
        assert result.returncode == 0 and seen[0]["protocol"] == "sigil-store/v1"
        assert seen[1]["status"] == "error"
        assert seen[2] == {"status": "ok", "page": {"keys": [], "next": None}}
    root = tmp_path / "old-v2"
    result, seen = run(legacy_native_store_binary, root, 2, [])
    assert result.returncode == 2 and seen == []
    assert list(root.iterdir()) == [], "old executable opened state before refusing profile v2"
