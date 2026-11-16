"""Each tenant's tools use only that tenant's credentials and allowlist, on the
real compiler (audit 2026-11-10, F-09).

A mock upstream stands in for GitHub and for any site ``fetch`` may reach, and
records every request that reaches it; a scripted provider stands in for the
model. Before this, the product handed every tenant the deployment's
PI_SECRET_GITHUB and PI_NET_ALLOWLIST: a tenant allowed ``gh_issues`` sent the
operator's token, and a tenant allowed ``fetch`` reached every host
allowlisted for anyone. Now each request carries its own tenant's token, a
tenant with none is refused by the runtime before any request leaves, and no
tenant's token reaches an audit record, a log line, an export, a metric or an
error body.
"""

import base64
import hashlib
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from conftest import API_KEY, PI_ROOT, msg, text, tool_use

import product_service
from agent import AuditLog, PiAgent, SessionStore
from product_service import (
    AuthRegistry,
    ProductDataManager,
    ProductScheduler,
    ProductScheduleStore,
    ProductService,
    SessionOperationLocks,
    _internal_session,
)


AUDIT_KEY = b"tenant-tool-grants-audit-key-0123456789abcdef"
# Canaries, shaped like the tokens they stand for. The underscores keep any
# secret scanner from taking them for real ones.
A_TOKEN = "ghp_a6_tenant_a_canary_5b1e07c4d9"
B_TOKEN = "ghp_a6_tenant_b_canary_8f2a63e1c7"
OPERATOR_TOKEN = "ghp_a6_operator_canary_3d9c41b8a2"
TOKENS = {name: f"{name}-credential-with-enough-entropy-for-a-test-fixture"
          for name in ("alice", "bob", "carol", "ops")}


def _entry(principal, tenant, scopes, tools=(), **policy):
    return {"sha256": hashlib.sha256(TOKENS[principal].encode()).hexdigest(),
            "principal": principal, "tenant": tenant, "scopes": list(scopes),
            "tools": list(tools), **policy}


def _bearer(principal):
    return {"Authorization": f"Bearer {TOKENS[principal]}",
            "X-Request-ID": "request-1234"}


@pytest.fixture()
def upstream():
    """Every host a tool may reach: ``POST /graphql`` answers as GitHub does
    (``/denied/graphql`` as it does to a bad token), anything else is a page.
    Records each request that arrives."""
    state = SimpleNamespace(requests=[], url=None)

    class Handler(BaseHTTPRequestHandler):
        def _serve(self):
            length = int(self.headers.get("Content-Length", 0) or 0)
            body = self.rfile.read(length) if length else b""
            state.requests.append(SimpleNamespace(
                method=self.command, path=self.path, body=body,
                headers={k.lower(): v for k, v in self.headers.items()}))
            status = 200
            if self.path == "/graphql":
                raw = json.dumps({"data": {"repository": {"issues": {
                    "totalCount": 1, "nodes": [{"number": 7, "title": "Tenant issue",
                                                "comments": {"totalCount": 0}}]}}}})
            elif self.path == "/denied/graphql":
                status, raw = 401, json.dumps({"message": "Bad credentials"})
            else:
                raw = "upstream page"
            raw = raw.encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        do_GET = _serve
        do_POST = _serve

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    state.url = f"http://127.0.0.1:{server.server_address[1]}"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield state
    server.shutdown()
    server.server_close()


def _manifest(tmp_path, base, graphql="/graphql"):
    """The shipped manifest, with gh_issues pointed at the mock upstream. Its
    secret grant and fetch's allowlist grant are the shipped ones."""
    manifest = json.loads((PI_ROOT / "tools" / "manifest.json").read_text())
    issues = manifest["gh_issues"]
    issues["bound_args"] = [f"{base}{graphql}"]
    issues["grants"] = {**issues["grants"], "net": ["127.0.0.1"]}
    assert issues["grants"]["secret"] == ["{SECRET:github}"]
    assert manifest["fetch"]["grants"] == {"net": ["{NET_ALLOWLIST}"]}
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    return path


def _product(tmp_path, mcp, provider, upstream, entries, *, tenant_secrets=None,
             deployment=False, log=None, graphql="/graphql"):
    """The product's own components over the real compiler. ``deployment``
    configures what product_main handed every tenant before A6: the
    deployment's GitHub token and allowlist (PI_SECRET_GITHUB and
    PI_NET_ALLOWLIST). A principal's turn must never use them."""
    for name in ("sessions", "sandboxes"):
        (tmp_path / name).mkdir(parents=True, exist_ok=True)
    agent = PiAgent(
        provider.url, API_KEY, store=SessionStore(tmp_path / "sessions"),
        sandbox_root=tmp_path / "sandboxes", mcp=mcp, model="claude-mock",
        manifest_path=_manifest(tmp_path, upstream.url, graphql),
        audit=AuditLog(tmp_path / "audit", key=AUDIT_KEY, strict=True),
        llm_retries=0,
        secrets={"github": OPERATOR_TOKEN} if deployment else None,
        net_allowlist=["127.0.0.1"] if deployment else None)
    auth = AuthRegistry(entries)
    schedules = ProductScheduleStore(tmp_path / "schedules.json")
    locks = SessionOperationLocks(tmp_path / "locks")
    data = ProductDataManager(agent, schedules, locks)
    extra = {} if tenant_secrets is None else {"tenant_secrets": tenant_secrets}
    service = ProductService(
        agent, auth, log_sink=log.append if log is not None else (lambda record: None),
        schedule_store=schedules, session_locks=locks, data_manager=data, **extra)
    return SimpleNamespace(agent=agent, auth=auth, service=service,
                           schedules=schedules, locks=locks, data=data, extra=extra)


def _issues_turn():
    return [msg([tool_use("t1", "gh_issues", {"repo": "octo/repo"})]), msg([text("done")])]


def _fetch_turn(upstream):
    return [msg([tool_use("t1", "fetch", {"url": f"{upstream.url}/page"})]),
            msg([text("done")])]


def _chat(product, provider, principal, session, script):
    provider.requests.clear()
    provider.script = list(script)
    status, _, body = product.service.dispatch(
        "POST", "/v1/chat", _bearer(principal),
        json.dumps({"session": session, "message": "go"}).encode())
    return status, body


def _tool_result(provider):
    """The tool result the model was shown at the turn's second step."""
    [result] = provider.requests[-1]["messages"][-1]["content"]
    assert result["type"] == "tool_result"
    return result


def _sent_tokens(upstream):
    return [request.headers.get("authorization") for request in upstream.requests]


def _tenant_secrets_dir(tmp_path, values):
    """PI_TENANT_SECRETS_DIR as an operator lays it out: one 0700 directory per
    tenant, named by the SHA-256 of the tenant, holding one 0600 file per
    secret."""
    root = tmp_path / "tenant-secrets"
    root.mkdir()
    root.chmod(0o700)
    for tenant, secrets in values.items():
        directory = root / hashlib.sha256(tenant.encode()).hexdigest()
        directory.mkdir()
        directory.chmod(0o700)
        for name, value in secrets.items():
            path = directory / name
            path.write_text(value + "\n")
            path.chmod(0o600)
    return root


# ── the audit's case ─────────────────────────────────────────────────────


def test_a_tenant_with_no_credential_of_its_own_never_uses_the_deployments(
        tmp_path, mcp, scripted_llm, upstream):
    """The deployment's token and allowlist are configured, as
    PI_SECRET_GITHUB and PI_NET_ALLOWLIST configured them; carol's policy
    allows gh_issues and fetch and names no secret and no host. Her tools are
    refused before any request leaves, instead of running with the
    operator's grants."""
    product = _product(tmp_path, mcp, scripted_llm, upstream,
                       [_entry("carol", "tenant-c", ["chat"], ["gh_issues", "fetch"])],
                       deployment=True)

    status, body = _chat(product, scripted_llm, "carol", "issues", _issues_turn())
    assert status == 200, body
    assert _sent_tokens(upstream) == [], (
        f"tenant-c's gh_issues went out with {_sent_tokens(upstream)}: the "
        f"operator's token (audit F-09)")
    result = _tool_result(scripted_llm)
    assert result.get("is_error") is True and "403" in result["content"], result

    status, body = _chat(product, scripted_llm, "carol", "page", _fetch_turn(upstream))
    assert status == 200, body
    assert upstream.requests == [], (
        "tenant-c's fetch reached a host allowlisted for the deployment (audit F-09)")
    result = _tool_result(scripted_llm)
    assert result.get("is_error") is True and "403" in result["content"], result


# ── each tenant's own ────────────────────────────────────────────────────


def test_each_tenants_gh_issues_sends_that_tenants_token(
        tmp_path, mcp, scripted_llm, upstream):
    tenant_secrets = product_service.TenantSecrets.from_directory(
        _tenant_secrets_dir(tmp_path, {"tenant-a": {"github": A_TOKEN},
                                       "tenant-b": {"github": B_TOKEN}}),
        AuthRegistry([
            _entry("alice", "tenant-a", ["chat"], ["gh_issues"], secrets=["github"]),
            _entry("bob", "tenant-b", ["chat"], ["gh_issues"], secrets=["github"]),
        ]).principals)
    product = _product(tmp_path, mcp, scripted_llm, upstream, [
        _entry("alice", "tenant-a", ["chat"], ["gh_issues"], secrets=["github"]),
        _entry("bob", "tenant-b", ["chat"], ["gh_issues"], secrets=["github"]),
        _entry("carol", "tenant-c", ["chat"], ["gh_issues"]),
    ], tenant_secrets=tenant_secrets, deployment=True)

    for principal, token in (("alice", A_TOKEN), ("bob", B_TOKEN)):
        upstream.requests.clear()
        status, body = _chat(product, scripted_llm, principal, "issues", _issues_turn())
        assert status == 200, body
        assert _sent_tokens(upstream) == [f"bearer {token}"], (
            f"{principal}'s gh_issues did not send its own tenant's token")
        result = _tool_result(scripted_llm)
        assert "is_error" not in result and "#7 Tenant issue" in result["content"], result

    upstream.requests.clear()
    status, body = _chat(product, scripted_llm, "carol", "issues", _issues_turn())
    assert status == 200, body
    assert upstream.requests == [], "a tenant with no token sent a request"
    result = _tool_result(scripted_llm)
    assert result.get("is_error") is True and "403" in result["content"], result


def test_each_tenants_fetch_reaches_only_the_hosts_its_policy_names(
        tmp_path, mcp, scripted_llm, upstream):
    product = _product(tmp_path, mcp, scripted_llm, upstream, [
        _entry("alice", "tenant-a", ["chat"], ["fetch"], net_allowlist=["127.0.0.1"]),
        _entry("bob", "tenant-b", ["chat"], ["fetch"],
               net_allowlist=["docs.example.com", "*.example.org"]),
        _entry("carol", "tenant-c", ["chat"], ["fetch"]),
    ], tenant_secrets=product_service.TenantSecrets(), deployment=True)

    status, body = _chat(product, scripted_llm, "alice", "page", _fetch_turn(upstream))
    assert status == 200, body
    assert [(r.method, r.path) for r in upstream.requests] == [("GET", "/page")]
    assert _tool_result(scripted_llm)["content"] == "upstream page"

    for principal in ("bob", "carol"):
        upstream.requests.clear()
        status, body = _chat(product, scripted_llm, principal, "page", _fetch_turn(upstream))
        assert status == 200, body
        assert upstream.requests == [], (
            f"{principal}'s fetch reached a host only another tenant may reach")
        result = _tool_result(scripted_llm)
        assert result.get("is_error") is True and "403" in result["content"], result


def test_a_scheduled_turn_uses_its_creators_current_grants(
        tmp_path, mcp, scripted_llm, upstream):
    entries = [_entry("bob", "tenant-b", ["chat", "schedules:write"], ["gh_issues"],
                      secrets=["github"])]
    tenant_secrets = product_service.TenantSecrets({"tenant-b": {"github": B_TOKEN}})
    product = _product(tmp_path, mcp, scripted_llm, upstream, entries,
                       tenant_secrets=tenant_secrets, deployment=True)
    bob = product.auth.policy(hashlib.sha256(b"tenant-b").hexdigest(), "bob")
    product.schedules.put(bob, name="nightly", session="nightly", message="go",
                          every_ms=60_000)
    scheduler = ProductScheduler(
        product.schedules, product.agent, product.auth, session_locks=product.locks,
        data_manager=product.data, log_sink=lambda record: None, **product.extra)
    scripted_llm.requests.clear()
    scripted_llm.script = _issues_turn()

    scheduler.tick()

    [entry] = product.schedules.for_tenant("tenant-b")
    assert entry["last_status"] == "ok", entry
    assert _sent_tokens(upstream) == [f"bearer {B_TOKEN}"]


# ── no token in anything the product writes or answers ───────────────────


def _forms(secret):
    """The forms a secret could take in what the product writes: as it is,
    base64 at each of the three alignments (an export carries file contents
    so), hex, and the SHA-256 the audit log uses for what it does not keep."""
    raw = secret.encode()
    forms = {raw, raw.hex().encode(), raw.hex().upper().encode(),
             hashlib.sha256(raw).hexdigest().encode(),
             hashlib.sha256(b"github=" + raw).hexdigest().encode()}
    for offset in range(3):
        for encode in (base64.b64encode, base64.urlsafe_b64encode):
            encoded = encode(b"\0" * offset + raw)
            # Only the characters no neighbouring byte can change.
            forms.add(encoded[0 if offset == 0 else 4:4 * ((offset + len(raw)) // 3)])
    return forms


def _leaks(blob, secrets):
    return sorted({secret for secret in secrets for form in _forms(secret) if form in blob})


@pytest.mark.parametrize("plant", [
    lambda s: f"prefix {s} suffix".encode(),
    lambda s: json.dumps({"grants": {"secret": [f"github={s}"]}}).encode(),
    lambda s: s.encode().hex().encode(),
    lambda s: s.encode().hex().upper().encode(),
    lambda s: hashlib.sha256(s.encode()).hexdigest().encode(),
    lambda s: hashlib.sha256(f"github={s}".encode()).hexdigest().encode(),
    *[(lambda offset: lambda s: json.dumps({"files": [{"content_base64": base64.b64encode(
        b"x" * offset + s.encode() + b"tail").decode()}]}).encode())(offset)
      for offset in range(3)],
    *[(lambda offset: lambda s: base64.urlsafe_b64encode(
        b"y" * offset + s.encode()).rstrip(b"="))(offset) for offset in range(3)],
], ids=["raw", "json", "hex", "HEX", "sha256", "grant-sha256",
        "base64+0", "base64+1", "base64+2", "urlsafe+0", "urlsafe+1", "urlsafe+2"])
def test_the_canary_detector_fires_on_every_form_it_looks_for(plant):
    """The scan below can only pass honestly if it can fail: each form of a
    planted token is found, and only that token."""
    blob = plant(A_TOKEN)
    assert _leaks(blob, [A_TOKEN, B_TOKEN]) == [A_TOKEN]
    assert _leaks(plant(B_TOKEN), [A_TOKEN]) == []


def _files_under(root, skip):
    for directory, _, names in os.walk(root):
        for name in names:
            path = os.path.join(directory, name)
            if not any(path.startswith(str(s)) for s in skip):
                with open(path, "rb") as handle:
                    yield path, handle.read()


def test_no_tenants_token_reaches_a_record_log_export_metric_or_error_body(
        tmp_path, mcp, scripted_llm, upstream, capfd):
    log = []
    secrets_dir = _tenant_secrets_dir(tmp_path, {"tenant-a": {"github": A_TOKEN},
                                                 "tenant-b": {"github": B_TOKEN}})
    entries = [
        _entry("alice", "tenant-a", ["chat", "sessions:read"], ["gh_issues", "fetch"],
               secrets=["github"], net_allowlist=["127.0.0.1"]),
        _entry("bob", "tenant-b", ["chat", "sessions:read", "schedules:write"],
               ["gh_issues"], secrets=["github"]),
        _entry("carol", "tenant-c", ["chat"], ["gh_issues"]),
        _entry("ops", "operations", ["ops:read"]),
    ]
    tenant_secrets = product_service.TenantSecrets.from_directory(
        secrets_dir, AuthRegistry(entries).principals)
    product = _product(tmp_path, mcp, scripted_llm, upstream, entries,
                       tenant_secrets=tenant_secrets, log=log)
    bodies = []

    def answer(status, body):
        bodies.append((status, body))
        return status

    # Turns that carry each token out, one that is refused, one that fails.
    assert answer(*_chat(product, scripted_llm, "alice", "work", _issues_turn())) == 200
    assert answer(*_chat(product, scripted_llm, "alice", "work", _fetch_turn(upstream))) == 200
    assert answer(*_chat(product, scripted_llm, "bob", "work", _issues_turn())) == 200
    assert answer(*_chat(product, scripted_llm, "carol", "work", _issues_turn())) == 200
    assert answer(*_chat(product, scripted_llm, "alice", "broken", [
        msg([tool_use("t1", "gh_issues", {"repo": "octo/repo"})]),
        {"not": "a model reply"}])) == 502
    assert _sent_tokens(upstream).count(f"bearer {A_TOKEN}") == 2
    assert _sent_tokens(upstream).count(f"bearer {B_TOKEN}") == 1
    # A scheduled turn, and an upstream that refuses the token it was sent.
    bob = product.auth.policy(hashlib.sha256(b"tenant-b").hexdigest(), "bob")
    product.schedules.put(bob, name="nightly", session="work", message="go",
                          every_ms=60_000)
    scripted_llm.requests.clear()
    scripted_llm.script = _issues_turn()
    ProductScheduler(product.schedules, product.agent, product.auth,
                     session_locks=product.locks, data_manager=product.data,
                     log_sink=log.append, **product.extra).tick()
    assert _sent_tokens(upstream).count(f"bearer {B_TOKEN}") == 2
    denied = _product(tmp_path / "denied", mcp, scripted_llm, upstream, entries,
                      tenant_secrets=tenant_secrets, log=log, graphql="/denied/graphql")
    assert answer(*_chat(denied, scripted_llm, "bob", "denied", _issues_turn())) == 200
    assert "401" in _tool_result(scripted_llm)["content"]
    # What a client can ask for afterwards.
    for principal, session in (("alice", "work"), ("alice", "broken"), ("bob", "work"),
                               ("alice", "never-used")):
        status, _, body = product.service.dispatch(
            "GET", f"/v1/sessions/{session}/export", _bearer(principal))
        bodies.append((status, body))
    for path in ("/v1/metrics", "/v1/ready", "/v1/health"):
        status, _, body = product.service.dispatch("GET", path, _bearer("ops"))
        bodies.append((status, body))
    status, _, payload = product.service.dispatch(
        "GET", "/v1/metrics/prometheus", _bearer("ops"))
    bodies.append((status, payload.body))
    statuses = [status for status, _ in bodies]
    assert statuses.count(502) == 1 and statuses.count(404) == 1, statuses

    captured = capfd.readouterr()
    outputs = [(f"answer {status}", (body if isinstance(body, str)
                                     else json.dumps(body, sort_keys=True)).encode())
               for status, body in bodies]
    outputs += [("log line", json.dumps(record, sort_keys=True).encode()) for record in log]
    outputs += [("stdout", captured.out.encode()), ("stderr", captured.err.encode()),
                ("compiler stderr", "\n".join(getattr(mcp, "_stderr_log", ())).encode())]
    # Every file the product wrote: sessions, sandboxes, audit chains,
    # schedules, locks. Not the secrets it was given, nor the manifest.
    written = list(_files_under(tmp_path, [secrets_dir, tmp_path / "manifest.json",
                                           tmp_path / "denied" / "manifest.json"]))
    assert any("audit" in path for path, _ in written), "no audit record was written"
    exports = [body["data"] for _, body in bodies
               if isinstance(body, dict) and "audit" in body.get("data", {})]
    assert len(exports) == 3 and all(export["audit"] for export in exports), (
        "an export with no audit record would make the scan of exports vacuous")
    outputs += written
    tokens = [A_TOKEN, B_TOKEN, OPERATOR_TOKEN]
    leaked = {where: _leaks(blob, tokens) for where, blob in outputs if _leaks(blob, tokens)}
    assert not leaked, f"a tenant's token was written or answered: {sorted(leaked)}"

    # The scan reads what it claims to: a token in a session's file is found
    # in that session's export, base64 and all.
    internal = _internal_session("tenant-a", "work")
    (product.agent.sandbox_for(internal) / "planted.txt").write_text(f"x{A_TOKEN}")
    status, _, export = product.service.dispatch(
        "GET", "/v1/sessions/work/export", _bearer("alice"))
    assert status == 200
    assert _leaks(json.dumps(export).encode(), tokens) == [A_TOKEN]
