"""Actual HTTP/native/SIGIL checks. Browser rendering is a separate required gate."""
from contextlib import closing
import hashlib
import json
import sqlite3
import subprocess

import pytest

from api_support import TOKEN_A, TOKEN_B, credential
from automatic_support import wait_operation
from browser_support import BrowserApi, assets, configure
from conftest import API_KEY
from test_turn_execution import programs as programs
from turn_support import text_reply, tool_reply


def state(root):
    with closing(sqlite3.connect(f"file:{root / 'records/records.sqlite'}?mode=ro", uri=True)) as db:
        return (db.execute("SELECT * FROM meta").fetchall(),
                db.execute("SELECT * FROM records ORDER BY namespace,key").fetchall())


def test_public_mounts_and_sigil_api_share_host_without_reads_starting_work(
        browser_service_binary, programs, scripted_llm, tmp_path, monkeypatch):
    monkeypatch.setenv("PI_AUTOMATIC_FIXTURE_SECRET", API_KEY)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = tmp_path / "service"
    with BrowserApi(browser_service_binary, root, programs, scripted_llm.url, workspace) as api:
        before = state(root)
        manifest = json.loads(api.asset_path.read_text())
        for item in manifest["assets"]:
            status, headers, body = api.raw(path=item["route"])
            assert status == 200
            assert hashlib.sha256(body).hexdigest() == item["sha256"]
            assert headers["content-type"] == item["content_type"] + "; charset=utf-8"
            assert headers["cache-control"] == "no-store"
            assert headers["referrer-policy"] == "no-referrer"
            assert headers["x-frame-options"] == "DENY"
            assert headers["x-content-type-options"] == "nosniff"
            assert "default-src 'none'" in headers["content-security-policy"]
            assert "connect-src 'self'" in headers["content-security-policy"]
            assert "frame-ancestors 'none'" in headers["content-security-policy"]
            assert "access-control-allow-origin" not in headers and "set-cookie" not in headers
            assert API_KEY.encode() not in body and TOKEN_A.encode() not in body
        assert api.request("GET", "/v1/sessions", token=None, raw=b"")[0] == 401
        assert api.request("GET", "/v1/sessions", raw=b"") == (200,
            {"sessions": [], "next_after": None, "order": "session_name", "consistency": "live_scan"})
        for path in ["/service.json", "/public-assets.json", "/_assets/service.json", "/_assets/../service.json", "/?token=canary"]:
            assert api.raw(path=path)[0] != 200
        for method in ["HEAD", "POST", "DELETE", "OPTIONS"]:
            assert api.raw(method)[0] == 405
        assert api.raw(payload=b"not-empty")[0] == 400
        assert state(root) == before
        assert scripted_llm.requests == []


def test_browser_origin_refusal_cannot_commit_even_with_an_actual_valid_token(
        browser_service_binary, programs, scripted_llm, tmp_path, monkeypatch):
    monkeypatch.setenv("PI_AUTOMATIC_FIXTURE_SECRET", API_KEY)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = tmp_path / "service"
    with BrowserApi(browser_service_binary, root, programs, scripted_llm.url, workspace) as api:
        before = state(root)
        host = ("Host", api.ready["address"])
        authorization = ("Authorization", "Bearer " + TOKEN_A)
        origin = ("Origin", "http://" + api.ready["address"])
        requests = [[], [("Host", "attacker.invalid")], [("Host", "localhost:" + str(api.port))],
            [host, host], [host, ("Origin", "null")], [host, ("Origin", "https://attacker.invalid")],
            [host, origin, origin], [host, ("Origin", "http://127.0.0.1:1")],
            [host, origin, ("Sec-Fetch-Site", "cross-site")], [host, origin, ("Sec-Fetch-Site", "same-site")],
            [host, origin, ("Sec-Fetch-Site", "same-origin"), ("Sec-Fetch-Site", "same-origin")]]
        body = json.dumps({"session": "notes", "message": "read", "submission_key": "one-key"}).encode()
        for headers in requests:
            status, returned, _ = api.raw("POST", "/v1/operations", payload=body, headers=[*headers, authorization])
            assert status in {400, 403}, headers  # Hyper may reject invalid HTTP before the handler.
            assert "access-control-allow-origin" not in returned
            assert state(root) == before
        assert scripted_llm.requests == []


def test_actual_model_tool_followup_two_tenants_and_reopen_with_browser_transport(
        browser_service_binary, programs, scripted_llm, tmp_path, monkeypatch):
    monkeypatch.setenv("PI_AUTOMATIC_FIXTURE_SECRET", API_KEY)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "README.md").write_text("The owner is Ada.\n")
    rows = [credential(), credential(TOKEN_B, principal="bob", tenant="tenant-b", prefix="b")]
    usage = {"input_tokens": 2, "output_tokens": 1}
    scripted_llm.script = [json.loads(tool_reply("read_file", usage=usage)), json.loads(text_reply("ALICE Ada canary.", usage=usage)),
                           json.loads(text_reply("ALICE follow-up canary.", usage=usage)), json.loads(text_reply("BOB private canary.", usage=usage))]
    root = tmp_path / "service"
    with BrowserApi(browser_service_binary, root, programs, scripted_llm.url, workspace, rows=rows) as api:
        saved = {}
        for token, key, message in [(TOKEN_A, "first", "Read README.md"), (TOKEN_A, "followup", "Who owns it?"),
                                     (TOKEN_B, "first", "Other tenant conversation")]:
            body = {"session": "same-name", "message": message, "submission_key": key}
            status, accepted = api.request(body=body, token=token)
            assert status == 202
            result = wait_operation(api, accepted["operation"], token=token)
            assert result["status"] == "done"
            saved[token] = result
        assert len(scripted_llm.requests) == 4
        assert scripted_llm.requests[1]["messages"][-1]["content"][0]["content"] == "The owner is Ada.\n"
        assert "ALICE Ada canary." in json.dumps(scripted_llm.requests[2])
        assert "ALICE" not in json.dumps(scripted_llm.requests[3])
        pages = {token: api.request("GET", "/v1/sessions/same-name/messages", token=token, raw=b"")
                 for token in [TOKEN_A, TOKEN_B]}
        assert pages[TOKEN_A][0] == pages[TOKEN_B][0] == 200
        assert "ALICE follow-up canary." in json.dumps(pages[TOKEN_A]) and "BOB" not in json.dumps(pages[TOKEN_A])
        assert "BOB private canary." in json.dumps(pages[TOKEN_B]) and "ALICE" not in json.dumps(pages[TOKEN_B])
        before = state(root)
        for token in [TOKEN_A, TOKEN_B]:
            assert api.request("GET", "/v1/sessions", token=token, raw=b"")[1]["sessions"][0]["session"] == "same-name"
        assert api.request("GET", "/v1/operations/" + saved[TOKEN_A]["operation"], token=TOKEN_B, raw=b"")[0] == 404
        assert state(root) == before
        asset_path = api.asset_path
    with BrowserApi(browser_service_binary, root, programs, scripted_llm.url, workspace, rows=rows, mode="open", asset_path=asset_path) as api:
        for token in [TOKEN_A, TOKEN_B]:
            assert api.request("GET", "/v1/sessions/same-name/messages", token=token, raw=b"") == pages[token]
        assert state(root) == before
    assert len(scripted_llm.requests) == 4


@pytest.mark.parametrize("change", ["digest", "version", "mime", "shadow_api", "missing", "too_large", "invalid_utf8"])
def test_bad_public_manifest_refuses_before_application_state_creation(
        browser_service_binary, programs, scripted_llm, tmp_path, change):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = tmp_path / "service"
    _, config_path = configure(root, programs, scripted_llm.url, workspace)
    path = assets(root)
    manifest = json.loads(path.read_text())
    if change == "digest":
        manifest["assets"][0]["sha256"] = "0" * 64
    elif change == "version":
        manifest["version"] = 2
    elif change == "mime":
        manifest["assets"][0]["content_type"] = "application/json"
    elif change == "shadow_api":
        manifest["assets"][0]["route"] = "/v1/operations"
    else:
        file = root / "public/index.html"
        if change == "missing":
            file.unlink()
        else:
            raw = b"x" * 65537 if change == "too_large" else b"\xff"
            file.write_bytes(raw)
            manifest["assets"][0]["sha256"] = hashlib.sha256(raw).hexdigest()
    path.write_text(json.dumps(manifest))
    result = subprocess.run([str(browser_service_binary), "init", str(config_path), "0", "--public-assets", str(path)],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 2 and result.stdout == ""
    assert json.loads(result.stderr) == {"status": "error", "code": "public_assets"}
    assert not (root / "records").exists()
    assert scripted_llm.requests == []


def test_frozen_older_host_refuses_opt_in_instead_of_silently_serving_an_unchecked_ui(
        legacy_native_release_service_binary, tmp_path):
    config = tmp_path / "service.json"
    config.write_text("{}")
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}")
    result = subprocess.run([str(legacy_native_release_service_binary), "init", str(config), "0", "--public-assets", str(manifest)],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 2 and result.stdout == ""
    assert json.loads(result.stderr) == {"status": "error", "code": "arguments"}
    assert sorted(path.name for path in tmp_path.iterdir()) == ["manifest.json", "service.json"]
