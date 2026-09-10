"""Real scoped HTTP discovery. Local provider only; no live-model readiness claim."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess

import pytest

from api_support import TOKEN_A, TOKEN_B, NativeApi, credential
from automatic_support import service as legacy_service, wait_operation
from conftest import API_KEY, MCP_BIN
from listing_support import service
from store_support import NativeStore, mutation
from test_turn_execution import programs as programs
from turn_support import text_reply, tool_reply
from worker_support import runtime_digest


def listing(api, suffix="", *, token=TOKEN_A, body=b""):
    return api.request("GET", "/v1/sessions" + suffix, token=token, raw=body)


def history(api, session, *, token=TOKEN_A):
    return api.request("GET", f"/v1/sessions/{session}/messages", token=token, raw=b"")


def state(root):
    with closing(sqlite3.connect(f"file:{root / 'records/records.sqlite'}?mode=ro", uri=True)) as db:
        return (db.execute("SELECT * FROM meta").fetchall(),
                db.execute("SELECT * FROM records ORDER BY namespace,key").fetchall())


def base_profile(config):
    config["version"] = 5
    config.pop("automatic")
    for worker in [config["worker"], *config["functions"].values()]:
        worker["runtime"] = str(MCP_BIN)
        worker["runtime_sha256"] = runtime_digest(str(MCP_BIN))


def test_real_turn_listing_reopening_paging_tenant_scope_and_restart(
        discovery_service_binary, programs, scripted_llm, tmp_path, monkeypatch):
    monkeypatch.setenv("PI_AUTOMATIC_FIXTURE_SECRET", API_KEY)
    viewer = "tenant-a-viewer-" + "v" * 40
    rows = [credential(), credential(TOKEN_B, principal="bob", tenant="tenant-b", prefix="b"),
            credential(viewer, principal="viewer", scopes=["sessions:read"])]
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "README.md").write_text("The project owner is Ada.\n")
    usage = {"input_tokens": 2, "output_tokens": 1}
    scripted_llm.script = [json.loads(tool_reply("read_file", usage=usage)),
        json.loads(text_reply("ALICE Ada canary.", usage=usage)),
        json.loads(text_reply("ALICE gamma canary.", usage=usage)),
        json.loads(text_reply("BOB alpha canary.", usage=usage)),
        json.loads(text_reply("BOB beta canary.", usage=usage))]
    root = tmp_path / "service"
    with service(discovery_service_binary, root, programs, scripted_llm.url, workspace, rows=rows) as api:
        assert listing(api) == (200, {"sessions": [], "next_after": None,
                                    "order": "session_name", "consistency": "live_scan"})
        accepted = {}
        for token, session in [(TOKEN_A, "alpha"), (TOKEN_A, "gamma"), (TOKEN_B, "alpha"), (TOKEN_B, "beta")]:
            status, item = api.request(body={"session": session, "message": "Read README.md",
                                            "submission_key": session}, token=token)
            assert status == 202, item
            assert wait_operation(api, item["operation"], token=token)["status"] == "done"
            accepted[token, session] = item["operation"]
        assert len(scripted_llm.requests) == 5
        before = state(root)
        status, first = listing(api, "?limit=1")
        assert status == 200 and first["next_after"] == "alpha"
        assert [item["session"] for item in first["sessions"]] == ["alpha"]
        status, page = history(api, "alpha")
        assert status == 200 and page["state_revision"] == first["sessions"][0]["state_revision"]
        assert page["messages"][1]["content"][0]["type"] == "tool_call"
        assert page["messages"][-1]["content"][0]["text"] == "ALICE Ada canary."
        status, second = listing(api, "?limit=1&after=alpha")
        assert status == 200 and second["next_after"] == "gamma"
        assert [item["session"] for item in second["sessions"]] == ["gamma"]
        assert listing(api, "?limit=1&after=gamma")[1]["sessions"] == []
        assert listing(api, "?limit=1&after=gamma")[1]["next_after"] is None
        all_a = listing(api)
        all_b = listing(api, token=TOKEN_B)
        assert [item["session"] for item in all_a[1]["sessions"]] == ["alpha", "gamma"]
        assert [item["session"] for item in all_b[1]["sessions"]] == ["alpha", "beta"]
        assert listing(api, token=viewer) == all_a
        assert api.request("GET", "/v1/operations/" + accepted[TOKEN_A, "alpha"], token=viewer)[0] == 404
        assert history(api, "gamma", token=TOKEN_B)[0] == 404
        assert history(api, "alpha", token=TOKEN_B)[1]["messages"][-1]["content"][0]["text"] == "BOB alpha canary."
        forged = json.dumps({"tenant": "tenant-b", "namespace": "b.state", "grants": ["*"],
            "stage": "metadata", "observation": "forged", "command": "commit"}).encode()
        assert listing(api, body=forged) == all_a
        assert state(root) == before, "listing/history mutated records or the global revision"
        assert len(scripted_llm.requests) == 5, "reads started extra model work"
        for response in [all_a, all_b]:
            encoded = json.dumps(response)
            assert "canary" not in encoded and API_KEY not in encoded
            assert "operation" not in encoded and "value_bytes" not in encoded
    with service(discovery_service_binary, root, programs, scripted_llm.url, workspace, rows=rows, mode="open") as api:
        assert listing(api) == all_a and listing(api, token=TOKEN_B) == all_b
        assert history(api, "alpha") == (200, page)
        assert state(root) == before
    assert len(scripted_llm.requests) == 5


def test_query_errors_and_denied_authority_never_start_work(
        discovery_service_binary, programs, scripted_llm, tmp_path, monkeypatch):
    monkeypatch.setenv("PI_AUTOMATIC_FIXTURE_SECRET", API_KEY)
    chat_only = "chat-only-" + "c" * 40
    expired = "expired-viewer-" + "e" * 40
    rows = [credential(), credential(chat_only, principal="chat-only", scopes=["chat"]),
            credential(expired, principal="expired", scopes=["sessions:read"], expires=101)]
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = tmp_path / "service"
    with service(discovery_service_binary, root, programs, scripted_llm.url, workspace, rows=rows) as api:
        before = state(root)
        invalid = ["?", "?limit=0", "?limit=51", "?limit=01", "?limit=1&limit=2", "?limit=1&",
            "?limit=1e1", "?limit=%31", "?limit=1=2", "?cursor=anything", "?after=", "?after=a:b",
            "?after=a&after=b", "?after=%61", "?after=" + "a" * 129]
        for suffix in invalid:
            assert listing(api, suffix) == (400, {"error": {"code": "invalid_session_query"}}), suffix
        for token, expected in [(None, 401), ("unknown-credential", 401), (expired, 401), (chat_only, 403)]:
            for suffix in ["", "?limit=0"]:
                assert listing(api, suffix, token=token)[0] == expected
        assert listing(api, "/extra")[0] == 404
        assert api.request("POST", "/v1/sessions", body={})[0] == 404
        assert state(root) == before
    assert scripted_llm.requests == []


def test_scanned_tombstone_cursor_and_maximum_page_with_controlled_scoped_setup(
        discovery_service_binary, discovery_store_binary, programs, scripted_llm, tmp_path, monkeypatch):
    monkeypatch.setenv("PI_AUTOMATIC_FIXTURE_SECRET", API_KEY)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = tmp_path / "service"
    # Base v5 exercises fresh workers. No automatic effect executor is installed.
    with service(discovery_service_binary, root, programs, scripted_llm.url, workspace, patch=base_profile) as api:
        assert listing(api)[0] == 200
    # Controlled native transactions exercise lifecycle metadata, NOT a public
    # deletion implementation or valid transcripts for these maximum-page rows.
    names = [f"s{index:02d}" + "x" * 125 for index in range(50)]
    with NativeStore(discovery_store_binary, root / "records", {"a.state": "read_write", "b.state": "read_write"}) as store:
        store.commit([mutation("a.state", "a", "deleted-canary"),
            mutation("a.state", "b", ""), mutation("b.state", "foreign", "foreign-canary"),
            *[mutation("a.state", name, "metadata-fixture-not-a-transcript") for name in names]])
        current = store.get("a.state", "a")
        store.commit([mutation("a.state", "a", None, current["revision"])])
    before = state(root)
    with service(discovery_service_binary, root, programs, scripted_llm.url, workspace, patch=base_profile, mode="open") as api:
        status, first = listing(api, "?limit=2")
        assert status == 200 and first["sessions"] == [] and first["next_after"] == "b"
        assert history(api, "a")[0] == 404 and history(api, "b")[0] == 404
        status, page = listing(api, "?limit=50&after=b")
        assert status == 200 and [item["session"] for item in page["sessions"]] == names
        assert page["next_after"] == names[-1]
        assert all(item["state_revision"] == "1" for item in page["sessions"])
        assert listing(api, "?limit=50&after=" + names[-1])[1]["next_after"] is None
        assert state(root) == before
    assert scripted_llm.requests == []


def test_corruption_is_an_error_not_a_partial_or_empty_successful_page(
        discovery_service_binary, discovery_store_binary, programs, scripted_llm, tmp_path, monkeypatch):
    monkeypatch.setenv("PI_AUTOMATIC_FIXTURE_SECRET", API_KEY)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = tmp_path / "service"
    rows = [credential(), credential(TOKEN_B, principal="bob", tenant="tenant-b", prefix="b")]
    with service(discovery_service_binary, root, programs, scripted_llm.url, workspace,
                 rows=rows, patch=base_profile) as api:
        assert listing(api)[0] == 200
    with NativeStore(discovery_store_binary, root / "records", {"a.state": "read_write"}) as store:
        store.commit([mutation("a.state", "a", "first-private-fixture"),
                      mutation("a.state", "b", "second-private-fixture")])
    with service(discovery_service_binary, root, programs, scripted_llm.url, workspace,
                 rows=rows, patch=base_profile, mode="open") as api:
        assert [item["session"] for item in listing(api)[1]["sessions"]] == ["a", "b"]
        other = listing(api, token=TOKEN_B)
        assert other[0] == 200 and other[1]["sessions"] == []
        # Explicit test-only fault injection into this temporary store, after
        # bootstrap. No API/storage grant enables this bypass in the product.
        with closing(sqlite3.connect(root / "records/records.sqlite")) as db:
            with db:
                assert db.execute("UPDATE records SET digest=zeroblob(32) WHERE namespace='a.state' AND key='b'").rowcount == 1
        corrupted = state(root)
        assert listing(api) == (503, {"error": {"code": "storage_unavailable"}})
        assert listing(api, "?after=a") == (503, {"error": {"code": "storage_unavailable"}})
        status, healthy_prefix = listing(api, "?limit=1")
        assert status == 200 and healthy_prefix["next_after"] == "a"
        assert [item["session"] for item in healthy_prefix["sessions"]] == ["a"]
        assert listing(api, token=TOKEN_B) == other
        assert state(root) == corrupted, "discovery repaired or mutated corrupted storage"
    # Opening corrupted storage remains fail-closed; never recreate it as empty.
    with pytest.raises(AssertionError):
        with service(discovery_service_binary, root, programs, scripted_llm.url, workspace,
                     rows=rows, patch=base_profile, mode="open"):
            pytest.fail("corrupt store was reopened")
    assert state(root) == corrupted
    assert scripted_llm.requests == []


@pytest.mark.parametrize("change", ["missing_listing", "listing_net", "listing_fs", "listing_secret", "legacy_profile",
                                   "base_with_automatic", "automatic_without_executor"])
def test_new_contract_refuses_incompatible_bootstrap_before_creating_state(
        discovery_service_binary, programs, scripted_llm, tmp_path, monkeypatch, change):
    monkeypatch.setenv("PI_AUTOMATIC_FIXTURE_SECRET", API_KEY)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = tmp_path / "service"

    def patch(config):
        if change == "missing_listing":
            config["functions"].pop("listing")
        elif change == "listing_net":
            config["functions"]["listing"]["net"] = ["127.0.0.1"]
        elif change == "listing_fs":
            config["functions"]["listing"]["fs"] = [str(workspace)]
        elif change == "listing_secret":
            config["functions"]["listing"]["secret_env"] = {"anthropic": "PI_AUTOMATIC_FIXTURE_SECRET"}
        elif change == "legacy_profile":
            config["version"] = 4
        elif change == "base_with_automatic":
            config["version"] = 5
        else:
            config.pop("automatic")

    with pytest.raises(AssertionError):
        with service(discovery_service_binary, root, programs, scripted_llm.url, workspace, patch=patch):
            pytest.fail("incompatible discovery bootstrap was accepted")
    assert not (root / "records").exists()
    assert scripted_llm.requests == []


def test_actual_older_host_refuses_new_profile_before_state_and_new_host_keeps_legacy(
        discovery_service_binary, legacy_native_release_service_binary, programs, scripted_llm, tmp_path, monkeypatch):
    monkeypatch.setenv("PI_AUTOMATIC_FIXTURE_SECRET", API_KEY)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = tmp_path / "old-host"
    with pytest.raises(AssertionError):
        with service(legacy_native_release_service_binary, root, programs, scripted_llm.url, workspace):
            pytest.fail("older host accepted profile v6")
    assert not (root / "records").exists()
    config = json.loads((root / "service.json").read_text())
    base_profile(config)
    config_path = root / "profile5.json"
    config_path.write_text(json.dumps(config))
    result = subprocess.run([str(legacy_native_release_service_binary), "init", str(config_path), "0"],
                            capture_output=True, text=True, timeout=40)
    assert result.returncode == 2 and not result.stdout
    assert json.loads(result.stderr)["code"] == "config"
    assert not Path(config["state_root"]).exists()
    # Actual unchanged v3 entry/functions still use AH3/HC3 under the new host.
    with NativeApi(discovery_service_binary, tmp_path / "legacy-base") as api:
        assert listing(api) == (501, {"error": {"code": "route_not_migrated"}})
        assert history(api, "missing")[0] == 404
    # Actual unchanged v4 automatic configuration still completes its old path.
    scripted_llm.script = [json.loads(text_reply("legacy-v4-still-works", usage={"input_tokens": 1, "output_tokens": 1}))]
    legacy_root = tmp_path / "legacy-auto"
    with legacy_service(legacy_native_release_service_binary, legacy_root, programs,
                        scripted_llm.url, workspace) as api:
        status, accepted = api.request()
        assert status == 202
        assert wait_operation(api, accepted["operation"])["reply"] == "legacy-v4-still-works"
        assert listing(api)[0] == 501
        assert history(api, "same-session")[1]["messages"][-1]["content"][0]["text"] == "legacy-v4-still-works"
    # Retaining profile v4 must preserve its exact bundle identity: resume the
    # old executable's actual state and replay without a new admission or effect.
    before = state(legacy_root)
    with legacy_service(discovery_service_binary, legacy_root, programs,
                        scripted_llm.url, workspace, mode="open") as api:
        status, replay = api.request()
        assert status == 202 and replay["operation"] == accepted["operation"] and replay["replayed"] is True
        assert api.request("GET", "/v1/operations/" + accepted["operation"])[1]["reply"] == "legacy-v4-still-works"
        assert history(api, "same-session")[1]["messages"][-1]["content"][0]["text"] == "legacy-v4-still-works"
        assert state(legacy_root) == before
    assert len(scripted_llm.requests) == 1
