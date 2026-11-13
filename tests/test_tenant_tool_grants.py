"""Tool credentials and reachable hosts belong to the tenant (audit
2026-11-10, F-09).

A principal's policy in the auth file names the secrets its tools may use and
the hosts its ``fetch`` may reach. The values come from the tenant's own
directory under PI_TENANT_SECRETS_DIR, read once at startup with its
permissions checked; the auth file still holds digests and names only. Both
turn paths hand the principal's grants to the agent, and a principal's turn
uses those and nothing else: never the deployment's PI_SECRET_* or
PI_NET_ALLOWLIST, which product mode now refuses at startup. The research host
keeps them. The real-compiler half of this is tests/test_tenant_tool_grants_forge.py.
"""

import ast
import hashlib
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from conftest import PI_ROOT

import product_main
import product_service
from agent import AuditLog, PiAgent, SessionStore
from product_service import (
    AuthRegistry,
    ConfigError,
    Principal,
    ProductScheduler,
    ProductScheduleStore,
    ProductService,
    SessionOperationLocks,
)
from test_product_main import _base_env, _install_happy_runtime


A_TOKEN = "ghp_a6_unit_tenant_a_value_0001"
B_TOKEN = "ghp_a6_unit_tenant_b_value_0002"
OPERATOR_TOKEN = "ghp_a6_unit_operator_value_0003"
TOKENS = {name: f"{name}-credential-with-enough-entropy-for-a-test-fixture"
          for name in ("alice", "alice-reader", "bob", "carol", "dave", "planner")}


def _entry(principal, tenant, scopes=("chat",), tools=(), **policy):
    return {"sha256": hashlib.sha256(TOKENS[principal].encode()).hexdigest(),
            "principal": principal, "tenant": tenant, "scopes": list(scopes),
            "tools": list(tools), **policy}


def _bearer(principal):
    return {"Authorization": f"Bearer {TOKENS[principal]}",
            "X-Request-ID": "request-1234"}


def _sha(tenant):
    return hashlib.sha256(tenant.encode()).hexdigest()


# ── the policy names grants; two credentials for one principal agree ─────


def test_a_policy_names_the_secrets_and_hosts_its_principal_may_use():
    registry = AuthRegistry([
        _entry("alice", "tenant-a", tools=["gh_issues", "fetch"], secrets=["github"],
               net_allowlist=["docs.example.com", "*.example.org", "127.0.0.1"]),
        _entry("carol", "tenant-c", tools=["fetch"]),
    ])
    alice = registry.authenticate(f"Bearer {TOKENS['alice']}")
    assert alice.secrets == frozenset({"github"})
    assert alice.net_allowlist == frozenset({"docs.example.com", "*.example.org",
                                             "127.0.0.1"})
    assert registry.policy(_sha("tenant-a"), "alice") == alice
    carol = registry.authenticate(f"Bearer {TOKENS['carol']}")
    assert carol.secrets == frozenset() and carol.net_allowlist == frozenset()


@pytest.mark.parametrize("field,value", [
    ("secrets", "github"),
    ("secrets", ["GitHub"]),
    ("secrets", ["git-hub"]),
    ("secrets", [""]),
    ("secrets", [7]),
    ("secrets", ["a" * 65]),
    ("secrets", ["../github"]),
    ("net_allowlist", "docs.example.com"),
    ("net_allowlist", ["*"]),
    ("net_allowlist", ["https://docs.example.com"]),
    ("net_allowlist", ["Docs.example.com"]),
    ("net_allowlist", ["docs.example.com:443"]),
    ("net_allowlist", ["docs.example.com/path"]),
    ("net_allowlist", [" docs.example.com"]),
    ("net_allowlist", ["docs example.com"]),
    ("net_allowlist", ["*."]),
    ("net_allowlist", ["docs..example.com"]),
    ("net_allowlist", [""]),
    ("net_allowlist", [None]),
    ("net_allowlist", ["a" * 64 + ".example.com"]),
    ("net_allowlist", [("a" * 60 + ".") * 5 + "com"]),
])
def test_a_malformed_grant_in_a_policy_is_a_configuration_error(field, value):
    with pytest.raises(ConfigError, match=f"auth entry 0 {field}"):
        AuthRegistry([_entry("alice", "tenant-a", **{field: value})])


@pytest.mark.parametrize("first,second", [
    ({"secrets": ["github"]}, {}),
    ({"secrets": ["github"]}, {"secrets": ["github", "gitlab"]}),
    ({"net_allowlist": ["docs.example.com"]}, {}),
    ({"net_allowlist": ["docs.example.com"]}, {"net_allowlist": ["*.example.com"]}),
])
def test_two_credentials_for_one_principal_must_agree_on_its_grants(first, second):
    """Either credential would otherwise widen what the other's holder may
    use, depending on which one the scheduler's lookup found first."""
    other = dict(_entry("alice", "tenant-a", **second),
                 sha256=hashlib.sha256(b"a second credential for alice").hexdigest())
    with pytest.raises(ConfigError, match="conflicts with another credential policy"):
        AuthRegistry([_entry("alice", "tenant-a", **first), other])


def test_the_order_a_policy_lists_its_grants_in_does_not_matter():
    other = dict(_entry("alice", "tenant-a", secrets=["gitlab", "github"],
                        net_allowlist=["b.example", "a.example"]),
                 sha256=hashlib.sha256(b"a second credential for alice").hexdigest())
    AuthRegistry([_entry("alice", "tenant-a", secrets=["github", "gitlab"],
                         net_allowlist=["a.example", "b.example"]), other])


# ── the tenant secret source ────────────────────────────────────────────


def _principal(tenant, *secrets, principal="p"):
    return Principal(principal, tenant, frozenset({"chat"}), frozenset(),
                     secrets=frozenset(secrets))


def _layout(tmp_path, values, *, root_mode=0o700, dir_mode=0o700, file_mode=0o600):
    """PI_TENANT_SECRETS_DIR: {tenant: {name: bytes}}."""
    root = tmp_path / "tenant-secrets"
    root.mkdir(parents=True)
    for tenant, secrets in values.items():
        directory = root / _sha(tenant)
        directory.mkdir()
        for name, value in secrets.items():
            path = directory / name
            path.write_bytes(value)
            path.chmod(file_mode)
        directory.chmod(dir_mode)
    root.chmod(root_mode)
    return root


def test_each_tenant_reads_its_own_directory_and_only_the_names_its_policies_name(
        tmp_path):
    root = _layout(tmp_path, {
        "tenant-a": {"github": A_TOKEN.encode() + b"\n", "gitlab": b"glpat_unused"},
        "tenant-b": {"github": B_TOKEN.encode() + b"\r\n"},
        "tenant-unconfigured": {"github": b"never read"},
    })
    alice, bob = _principal("tenant-a", "github"), _principal("tenant-b", "github")
    reader = _principal("tenant-a", principal="reader")
    loaded = product_service.TenantSecrets.from_directory(root, [alice, bob, reader])
    assert loaded.for_principal(alice) == {"github": A_TOKEN}
    assert loaded.for_principal(bob) == {"github": B_TOKEN}
    # The scope is the policy: a principal that names nothing gets nothing,
    # whatever its tenant holds.
    assert loaded.for_principal(reader) == {}
    # What no policy names is not read at all.
    assert loaded.missing(_principal("tenant-a", "gitlab")) == ["gitlab"]
    assert product_service.tenant_secret_path(root, "tenant-a") == root / _sha("tenant-a")


def test_the_loaded_values_are_never_shown(tmp_path):
    root = _layout(tmp_path, {"tenant-a": {"github": A_TOKEN.encode()}})
    loaded = product_service.TenantSecrets.from_directory(
        root, [_principal("tenant-a", "github")])
    for shown in (repr(loaded), str(loaded)):
        assert A_TOKEN not in shown and "tenant-a" not in shown


def test_without_a_directory_only_policies_that_name_no_secret_are_served(tmp_path):
    loaded = product_service.TenantSecrets.from_directory(
        None, [_principal("tenant-a")])
    assert loaded.for_principal(_principal("tenant-a")) == {}
    with pytest.raises(ConfigError, match="PI_TENANT_SECRETS_DIR") as caught:
        product_service.TenantSecrets.from_directory(
            None, [_principal("tenant-a", "github", principal="alice")])
    assert "alice" in str(caught.value) and "github" in str(caught.value)


def test_a_named_secret_without_a_value_names_the_file_it_expected(tmp_path):
    root = _layout(tmp_path, {"tenant-a": {"gitlab": b"glpat_x"}})
    for principal, path in (
            (_principal("tenant-a", "github", principal="alice"),
             root / _sha("tenant-a") / "github"),
            (_principal("tenant-b", "github", principal="bob"), root / _sha("tenant-b"))):
        with pytest.raises(ConfigError) as caught:
            product_service.TenantSecrets.from_directory(root, [principal])
        message = str(caught.value)
        assert str(path) in message and principal.tenant_id in message, message


@pytest.mark.parametrize("modes", [
    {"root_mode": 0o750}, {"root_mode": 0o705}, {"root_mode": 0o720},
    {"dir_mode": 0o710}, {"dir_mode": 0o701}, {"dir_mode": 0o770},
    {"file_mode": 0o640}, {"file_mode": 0o604}, {"file_mode": 0o620},
])
def test_a_secret_any_other_user_could_read_or_replace_is_refused(tmp_path, modes):
    root = _layout(tmp_path, {"tenant-a": {"github": A_TOKEN.encode()}}, **modes)
    with pytest.raises(ConfigError, match="must not be accessible") as caught:
        product_service.TenantSecrets.from_directory(
            root, [_principal("tenant-a", "github")])
    assert A_TOKEN not in str(caught.value)
    assert oct(next(iter(modes.values())))[2:] in str(caught.value)


def test_a_secret_owned_by_another_user_is_refused(tmp_path, monkeypatch):
    root = _layout(tmp_path, {"tenant-a": {"github": A_TOKEN.encode()}})
    someone_else = os.geteuid() + 1
    monkeypatch.setattr(product_service.os, "geteuid", lambda: someone_else)
    with pytest.raises(ConfigError, match="owned by"):
        product_service.TenantSecrets.from_directory(
            root, [_principal("tenant-a", "github")])


def test_a_symbolic_link_inside_the_source_is_refused(tmp_path):
    """A link's target lies outside the directory whose permissions were
    checked, so following it would make the check say nothing."""
    elsewhere = _layout(tmp_path / "elsewhere", {"tenant-b": {"github": B_TOKEN.encode()}})
    root = _layout(tmp_path, {"tenant-a": {}})
    (root / _sha("tenant-a")).chmod(0o700)
    (root / _sha("tenant-a") / "github").symlink_to(elsewhere / _sha("tenant-b") / "github")
    with pytest.raises(ConfigError, match="symbolic link"):
        product_service.TenantSecrets.from_directory(
            root, [_principal("tenant-a", "github")])
    (root / _sha("tenant-c")).symlink_to(elsewhere / _sha("tenant-b"))
    with pytest.raises(ConfigError, match="symbolic link"):
        product_service.TenantSecrets.from_directory(
            root, [_principal("tenant-c", "github")])


def test_the_source_itself_may_be_reached_through_a_link(tmp_path):
    """Only what lies inside is held to the no-link rule: the operator names
    the root, and a path such as /tmp or /var is itself a link on macOS."""
    root = _layout(tmp_path, {"tenant-a": {"github": A_TOKEN.encode()}})
    (tmp_path / "mounted").symlink_to(root)
    loaded = product_service.TenantSecrets.from_directory(
        tmp_path / "mounted", [_principal("tenant-a", "github")])
    assert loaded.for_principal(_principal("tenant-a", "github")) == {"github": A_TOKEN}


def test_a_secret_that_is_not_a_regular_file_is_refused(tmp_path):
    root = _layout(tmp_path, {"tenant-a": {}, "tenant-b": {}})
    for tenant in ("tenant-a", "tenant-b"):
        (root / _sha(tenant)).chmod(0o700)
    (root / _sha("tenant-a") / "github").mkdir(mode=0o700)
    os.mkfifo(root / _sha("tenant-b") / "github", 0o600)
    for tenant in ("tenant-a", "tenant-b"):
        with pytest.raises(ConfigError, match="regular file"):
            product_service.TenantSecrets.from_directory(
                root, [_principal(tenant, "github")])


@pytest.mark.parametrize("value,problem", [
    (b"", "empty"),
    (b"\n", "empty"),
    (b"CANARY with a space", "printable"),
    (b"CANARY\ttab", "printable"),
    (b"CANARY\nsecond-line", "printable"),
    (b"CANARY\x00nul", "printable"),
    (b"CANARY\xff", "printable"),
    (b"CANARY" + b"x" * 4091, "longer than 4096 bytes"),
])
def test_a_value_that_could_not_be_a_credential_is_refused_without_showing_it(
        tmp_path, value, problem):
    root = _layout(tmp_path, {"tenant-a": {"github": value}})
    with pytest.raises(ConfigError, match=problem) as caught:
        product_service.TenantSecrets.from_directory(
            root, [_principal("tenant-a", "github")])
    assert "CANARY" not in str(caught.value)


def test_the_longest_value_accepted_is_4096_bytes(tmp_path):
    root = _layout(tmp_path, {"tenant-a": {"github": b"x" * 4096 + b"\n"}})
    loaded = product_service.TenantSecrets.from_directory(
        root, [_principal("tenant-a", "github")])
    assert loaded.for_principal(_principal("tenant-a", "github")) == {"github": "x" * 4096}


def test_a_tenant_entry_that_is_not_a_directory_is_refused(tmp_path):
    root = _layout(tmp_path, {})
    (root / _sha("tenant-a")).write_text("not a directory")
    (root / _sha("tenant-a")).chmod(0o600)
    with pytest.raises(ConfigError, match="is not a directory"):
        product_service.TenantSecrets.from_directory(
            root, [_principal("tenant-a", "github")])


@pytest.mark.parametrize("refused", ["looking", "reading"])
def test_a_secret_the_service_may_not_read_is_refused_by_name(
        tmp_path, monkeypatch, refused):
    """Looking at it or reading it failing (EACCES here) is a configuration
    error that names the path and shows nothing of the value."""
    root = _layout(tmp_path, {"tenant-a": {"github": A_TOKEN.encode()}})
    path = root / _sha("tenant-a") / "github"
    real_lstat = os.lstat

    def denied(*args, **kwargs):
        raise PermissionError(13, "Permission denied")

    if refused == "looking":
        monkeypatch.setattr(product_service.os, "lstat", lambda target: (
            denied() if Path(target) == path else real_lstat(target)))
    else:
        monkeypatch.setattr(product_service, "open", denied, raising=False)
    with pytest.raises(ConfigError, match=r"cannot be read \(EACCES\)") as caught:
        product_service.TenantSecrets.from_directory(
            root, [_principal("tenant-a", "github")])
    assert str(path) in str(caught.value) and A_TOKEN not in str(caught.value)


def test_a_secret_replaced_between_its_check_and_its_read_is_refused(
        tmp_path, monkeypatch):
    """The file read must be the file whose owner and mode were checked."""
    root = _layout(tmp_path, {"tenant-a": {"github": A_TOKEN.encode()}})
    real_fstat = os.fstat

    def another_file(fd):
        info = real_fstat(fd)
        return os.stat_result((info.st_mode, info.st_ino + 1, *tuple(info)[2:10]))

    monkeypatch.setattr(product_service.os, "fstat", another_file)
    with pytest.raises(ConfigError, match="was replaced while it was read"):
        product_service.TenantSecrets.from_directory(
            root, [_principal("tenant-a", "github")])


def test_asking_for_a_value_the_tenant_lacks_names_it_and_shows_nothing():
    held = product_service.TenantSecrets({"tenant-a": {"gitlab": A_TOKEN}})
    with pytest.raises(KeyError) as caught:
        held.for_principal(_principal("tenant-a", "github", "gitlab"))
    assert caught.value.args == (["github"],)
    assert A_TOKEN not in repr(caught.value)


@pytest.mark.parametrize("make", ["missing", "file"])
def test_a_source_that_is_not_a_directory_names_the_setting(tmp_path, make):
    root = tmp_path / "tenant-secrets"
    if make == "file":
        root.write_text("not a directory")
    with pytest.raises(ConfigError, match="PI_TENANT_SECRETS_DIR"):
        product_service.TenantSecrets.from_directory(
            root, [_principal("tenant-a", "github")])


# ── both turn paths hand the agent the principal's grants ────────────────


class GrantRecordingAgent:
    manifest = {"gh_issues": {}, "fetch": {}}
    memory = None
    _mcp = object()

    def __init__(self):
        self.calls = []

    def turn_with_usage(self, session, message, *, allowed_tools=None,
                        deadline_monotonic=None, **grants):
        self.calls.append(SimpleNamespace(session=session, grants=grants,
                                          tools=set(allowed_tools or ())))
        return "answer", {"input_tokens": 1, "output_tokens": 1}

    def turn_telemetry(self):
        return {"forge_queue_wait_ms": 0.0, "tool_calls": 0, "retries": 0}


SERVICE_ENTRIES = [
    _entry("alice", "tenant-a", tools=["gh_issues", "fetch"], secrets=["github"],
           net_allowlist=["a.example"]),
    _entry("alice-reader", "tenant-a", tools=["gh_issues"]),
    _entry("bob", "tenant-b", tools=["gh_issues"], secrets=["github"]),
    _entry("carol", "tenant-c", tools=["gh_issues", "fetch"]),
    _entry("dave", "tenant-d", tools=["fetch"], net_allowlist=["*.d.example"]),
]


def _tenant_values():
    return product_service.TenantSecrets({
        "tenant-a": {"github": A_TOKEN, "gitlab": "glpat_a6_unit_not_named"},
        "tenant-b": {"github": B_TOKEN},
    })


def test_a_chat_turn_is_given_its_principals_grants_and_no_others():
    agent = GrantRecordingAgent()
    service = ProductService(agent, AuthRegistry(SERVICE_ENTRIES),
                             log_sink=lambda record: None,
                             tenant_secrets=_tenant_values())
    expected = {
        "alice": {"secrets": {"github": A_TOKEN}, "net_allowlist": ["a.example"]},
        "alice-reader": {},
        "bob": {"secrets": {"github": B_TOKEN}},
        "carol": {},
        "dave": {"net_allowlist": ["*.d.example"]},
    }
    for principal, grants in expected.items():
        status, _, body = service.dispatch(
            "POST", "/v1/chat", _bearer(principal),
            json.dumps({"session": "s", "message": "go"}).encode())
        assert status == 200, body
        assert agent.calls[-1].grants == grants, principal


@pytest.mark.parametrize("values", [
    None, {}, {"tenant-a": {"gitlab": "CANARY_a6_other_name"}},
    {"tenant-b": {"github": "CANARY_a6_other_tenant"}},
], ids=["no-source", "empty", "other-name", "other-tenant"])
def test_a_policy_naming_a_secret_its_tenant_lacks_is_a_startup_error(values):
    tenant_secrets = None if values is None else product_service.TenantSecrets(values)
    with pytest.raises(ConfigError, match=r"alice.*github") as caught:
        ProductService(GrantRecordingAgent(), AuthRegistry(SERVICE_ENTRIES[:1]),
                       log_sink=lambda record: None, tenant_secrets=tenant_secrets)
    assert "CANARY" not in str(caught.value)


def test_an_embedder_that_drops_a_tenants_secret_gets_a_configuration_error():
    service = ProductService(GrantRecordingAgent(), AuthRegistry(SERVICE_ENTRIES[:1]),
                             log_sink=lambda record: None,
                             tenant_secrets=_tenant_values())
    service.tenant_secrets = product_service.TenantSecrets()
    status, _, body = service.dispatch(
        "POST", "/v1/chat", _bearer("alice"),
        json.dumps({"session": "s", "message": "go"}).encode())
    assert (status, body["error"]["code"]) == (503, "configuration_error")
    assert service.agent.calls == []


class _Clock:
    def __init__(self):
        self.now = time.time()

    def time(self):
        return self.now


def test_a_scheduled_turn_is_given_its_creators_current_grants(tmp_path):
    planner = _entry("planner", "tenant-a", ["schedules:write"], ["gh_issues", "fetch"],
                     secrets=["github"], net_allowlist=["a.example"])
    created = AuthRegistry([planner])
    store = ProductScheduleStore(tmp_path / "schedules.json")
    store.put(created.authenticate(f"Bearer {TOKENS['planner']}"), name="nightly",
              session="s", message="go", every_ms=60_000)
    agent, clock = GrantRecordingAgent(), _Clock()

    def run(auth, tenant_secrets):
        clock.now += 61
        ProductScheduler(store, agent, auth, clock=clock,
                         session_locks=SessionOperationLocks(tmp_path / "locks"),
                         log_sink=lambda record: None,
                         tenant_secrets=tenant_secrets).tick()
        [entry] = store.for_tenant("tenant-a")
        return entry["last_status"]

    assert run(created, _tenant_values()) == "ok"
    assert agent.calls[-1].grants == {"secrets": {"github": A_TOKEN},
                                      "net_allowlist": ["a.example"]}
    # The operator narrows the policy: the next run has what the creator may
    # use now, not what it could when the schedule was made.
    narrowed = AuthRegistry([dict(planner, secrets=[], net_allowlist=["b.example"])])
    assert run(narrowed, _tenant_values()) == "ok"
    assert agent.calls[-1].grants == {"net_allowlist": ["b.example"]}
    # A policy naming a secret its tenant has no value for runs nothing.
    assert run(created, product_service.TenantSecrets()) == "policy_invalid"
    assert len(agent.calls) == 2


# ── the agent: a principal's turn uses its own grants and nothing else ──


def _stub_agent(tmp_path, *, secrets=None, net_allowlist=None):
    """A PiAgent whose model and forges are scripted: it records the grants
    each tool would be forged with. The shipped manifest, without the shaping
    stage (which needs SIGIL's stdlib to compose)."""
    manifest = json.loads((PI_ROOT / "tools" / "manifest.json").read_text())
    for entry in manifest.values():
        entry.pop("shape", None)
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    for name in ("sessions", "sandboxes"):
        (tmp_path / name).mkdir(exist_ok=True)
    agent = PiAgent("http://127.0.0.1:9/v1/messages", "unused",
                    store=SessionStore(tmp_path / "sessions"),
                    sandbox_root=tmp_path / "sandboxes", manifest_path=path,
                    audit=AuditLog(tmp_path / "audit", enabled=False),
                    secrets=secrets, net_allowlist=net_allowlist)
    agent.forged = []

    def forge(source, input_text, grants, *args, **kwargs):
        agent.forged.append((kwargs["kind"], grants))
        return "tool output", None

    def script(*tools):
        steps = iter([
            [*(("tool_use", f"t{i}", name,
                {arg: "octo/repo" for arg in manifest[name]["args"]})
               for i, name in enumerate(tools)), ("usage", 1, 1), ("stop", "tool_use")],
            [("text", "done"), ("usage", 1, 1), ("stop", "end_turn")],
        ])
        agent._reply = lambda payload, session: next(steps)

    agent._forge = forge
    agent.script = script
    return agent


def _tool_grants(agent):
    return {kind[len("tool:"):]: grants for kind, grants in agent.forged}


DEPLOYMENT = {"secrets": {"github": OPERATOR_TOKEN, "gitlab": "glpat_a6_operator"},
              "net_allowlist": ["docs.example.com"]}


def test_a_principals_turn_never_uses_the_deployments_grants(tmp_path):
    agent = _stub_agent(tmp_path, **DEPLOYMENT)
    agent.script("gh_issues", "gl_issues", "fetch")
    agent.turn_with_usage("s", "go", allowed_tools={"gh_issues", "gl_issues", "fetch"})
    assert _tool_grants(agent) == {
        "gh_issues": {"net": ["api.github.com"], "secret": []},
        "gl_issues": {"net": ["gitlab.com"], "secret": []},
        "fetch": {"net": []},
    }


def test_a_principals_turn_uses_the_grants_it_was_given(tmp_path):
    agent = _stub_agent(tmp_path, **DEPLOYMENT)
    agent.script("gh_issues", "gl_issues", "fetch")
    agent.turn_with_usage("s", "go", allowed_tools={"gh_issues", "gl_issues", "fetch"},
                          secrets={"github": A_TOKEN},
                          net_allowlist=["a.example", "*.b.example"])
    assert _tool_grants(agent) == {
        "gh_issues": {"net": ["api.github.com"], "secret": [f"github={A_TOKEN}"]},
        "gl_issues": {"net": ["gitlab.com"], "secret": []},
        "fetch": {"net": ["a.example", "*.b.example"]},
    }


def test_the_research_host_keeps_the_deployments_grants_unless_given_its_own(tmp_path):
    agent = _stub_agent(tmp_path, **DEPLOYMENT)
    agent.script("gh_issues", "fetch")
    agent.turn_with_usage("s", "go")
    assert _tool_grants(agent) == {
        "gh_issues": {"net": ["api.github.com"], "secret": [f"github={OPERATOR_TOKEN}"]},
        "fetch": {"net": ["docs.example.com"]},
    }
    agent.forged.clear()
    agent.script("gh_issues", "fetch")
    agent.turn_with_usage("s", "go", secrets={}, net_allowlist=["a.example"])
    assert _tool_grants(agent) == {
        "gh_issues": {"net": ["api.github.com"], "secret": []},
        "fetch": {"net": ["a.example"]},
    }


@pytest.mark.parametrize("grants", [
    {"net_allowlist": "a.example"},
    {"net_allowlist": 7},
    {"net_allowlist": [7]},
    {"secrets": {"github": 7}},
    {"secrets": {7: "value"}},
    {"secrets": ["github"]},
])
def test_turn_grants_of_the_wrong_shape_stop_the_turn_before_it_starts(tmp_path, grants):
    agent = _stub_agent(tmp_path)
    agent.script("fetch")
    with pytest.raises(TypeError, match="secrets|net_allowlist"):
        agent.turn_with_usage("s", "go", allowed_tools={"fetch"}, **grants)
    assert agent.forged == []
    assert agent.store.load("s") == []


NAMES = st.sampled_from(["github", "gitlab", "notion"])
HOSTS = st.sampled_from(["a.example", "b.example", "*.c.example", "127.0.0.1"])
VALUES = st.text(alphabet="abcdefghij0123456789_", min_size=1, max_size=12)


@settings(max_examples=60, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(deployment_secrets=st.dictionaries(NAMES, VALUES.map("deploy_".__add__)),
       deployment_hosts=st.lists(st.just("deploy.example"), max_size=1),
       turn_secrets=st.none() | st.dictionaries(NAMES, VALUES.map("turn_".__add__)),
       turn_hosts=st.none() | st.lists(HOSTS, max_size=3),
       tools=st.lists(st.sampled_from(["gh_issues", "gl_issues", "fetch"]),
                      min_size=1, max_size=4))
def test_every_tool_of_a_principals_turn_is_forged_with_only_that_turns_grants(
        tmp_path, deployment_secrets, deployment_hosts, turn_secrets, turn_hosts, tools):
    agent = _stub_agent(tmp_path / os.urandom(6).hex(), secrets=deployment_secrets,
                        net_allowlist=deployment_hosts)
    agent.script(*tools)
    grants = {}
    if turn_secrets is not None:
        grants["secrets"] = turn_secrets
    if turn_hosts is not None:
        grants["net_allowlist"] = turn_hosts
    agent.turn_with_usage("s", "go", allowed_tools={"gh_issues", "gl_issues", "fetch"},
                          **grants)
    assert [kind for kind, _ in agent.forged] == [f"tool:{name}" for name in tools]
    names = {"gh_issues": "github", "gl_issues": "gitlab"}
    for (kind, forged), name in zip(agent.forged, tools):
        if name == "fetch":
            assert forged == {"net": list(turn_hosts or [])}
        else:
            value = (turn_secrets or {}).get(names[name])
            assert forged["secret"] == ([f"{names[name]}={value}"] if value else [])
        assert "deploy" not in json.dumps(forged)


# ── product mode refuses the deployment-wide grants; reads the tenants' ──


def _auth_file(tmp_path, monkeypatch, **policy):
    token = "startup-test-token-with-high-entropy-placeholder"
    path = tmp_path / "auth-a6.json"
    path.write_text(json.dumps({"tokens": [{
        "sha256": hashlib.sha256(token.encode()).hexdigest(),
        "principal": "user", "tenant": "tenant", "scopes": ["chat"], "tools": [],
        **policy, "not_before_unix": int(time.time()) - 60,
        "expires_unix": int(time.time()) + 86400}]}))
    monkeypatch.setenv("PI_AUTH_FILE", str(path))


@pytest.mark.parametrize("name", ["PI_SECRET_GITHUB", "PI_SECRET_github",
                                  "PI_SECRET_NOTION"])
def test_product_mode_refuses_a_deployment_wide_tool_secret(tmp_path, monkeypatch, name):
    _base_env(tmp_path, monkeypatch)
    # Doubles, so that a startup that does not refuse ends instead of serving.
    _install_happy_runtime(monkeypatch)
    monkeypatch.setenv(name, OPERATOR_TOKEN)
    with pytest.raises(ConfigError) as caught:
        product_main.run()
    message = str(caught.value)
    for replacement in (name, '"secrets"', "PI_AUTH_FILE", "PI_TENANT_SECRETS_DIR"):
        assert replacement in message, message
    assert OPERATOR_TOKEN not in message
    assert not (tmp_path / "state").exists()


def test_product_mode_refuses_a_deployment_wide_allowlist(tmp_path, monkeypatch):
    _base_env(tmp_path, monkeypatch)
    _install_happy_runtime(monkeypatch)
    monkeypatch.setenv("PI_NET_ALLOWLIST", "docs.example.com")
    with pytest.raises(ConfigError) as caught:
        product_main.run()
    message = str(caught.value)
    for replacement in ("PI_NET_ALLOWLIST", '"net_allowlist"', "PI_AUTH_FILE"):
        assert replacement in message, message
    assert not (tmp_path / "state").exists()


def _run_recording(monkeypatch):
    """product_main.run() over the doubles test_product_main uses, keeping
    what the agent and the scheduler were given and the service it built."""
    seen = {}
    _install_happy_runtime(monkeypatch, during=lambda service: seen.update(service=service))
    agent_double, scheduler_double = product_main.PiAgent, product_main.ProductScheduler

    class Agent(agent_double):
        def __init__(self, endpoint, api_key, **kwargs):
            seen["agent"] = kwargs
            super().__init__(endpoint, api_key, **kwargs)

    class Scheduler(scheduler_double):
        def __init__(self, *args, **kwargs):
            seen["scheduler"] = kwargs
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(product_main, "PiAgent", Agent)
    monkeypatch.setattr(product_main, "ProductScheduler", Scheduler)
    product_main.run()
    return seen


def test_a_set_but_empty_deployment_grant_is_not_configured(tmp_path, monkeypatch):
    """The research host reads both so (secrets_from_env, _parse_allowlist),
    and an operator's blank line shares nothing."""
    _base_env(tmp_path, monkeypatch)
    monkeypatch.setenv("PI_SECRET_GITHUB", "")
    monkeypatch.setenv("PI_NET_ALLOWLIST", " , ")
    seen = _run_recording(monkeypatch)
    assert (seen["agent"]["secrets"], seen["agent"]["net_allowlist"]) == ({}, [])


def test_product_mode_reads_each_tenants_secrets_and_gives_both_turn_paths_them(
        tmp_path, monkeypatch):
    _base_env(tmp_path, monkeypatch)
    _auth_file(tmp_path, monkeypatch, secrets=["github"], net_allowlist=["a.example"])
    root = _layout(tmp_path, {"tenant": {"github": A_TOKEN.encode() + b"\n"}})
    monkeypatch.setenv("PI_TENANT_SECRETS_DIR", str(root))
    seen = _run_recording(monkeypatch)
    service = seen["service"]
    [user] = service.auth.principals
    assert service.tenant_secrets.for_principal(user) == {"github": A_TOKEN}
    assert seen["scheduler"]["tenant_secrets"] is service.tenant_secrets
    # Nothing deployment-wide for a principal's turn to fall back to.
    assert (seen["agent"]["secrets"], seen["agent"]["net_allowlist"]) == ({}, [])
    # The values stay in this process's memory: the compiler, which inherits
    # the environment, sees the directory's path and no value.
    assert A_TOKEN not in json.dumps(dict(os.environ))


def test_a_policy_naming_a_secret_with_no_value_stops_startup_before_any_state(
        tmp_path, monkeypatch):
    _base_env(tmp_path, monkeypatch)
    _auth_file(tmp_path, monkeypatch, secrets=["github"])
    monkeypatch.delenv("PI_TENANT_SECRETS_DIR", raising=False)
    _install_happy_runtime(monkeypatch)
    with pytest.raises(ConfigError, match="PI_TENANT_SECRETS_DIR"):
        product_main.run()
    root = _layout(tmp_path, {"tenant": {"gitlab": b"glpat_a6"}})
    monkeypatch.setenv("PI_TENANT_SECRETS_DIR", str(root))
    with pytest.raises(ConfigError) as caught:
        product_main.run()
    assert str(root / _sha("tenant") / "github") in str(caught.value)
    assert not (tmp_path / "state").exists()


# ── guards ───────────────────────────────────────────────────────────────


def _functions(path):
    tree = ast.parse(Path(path).read_text())
    return [node for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]


def test_only_one_agent_function_reads_the_deployments_tool_grants():
    """Every tool's grants come through PiAgent._tool_grants, the one place
    that decides a principal's turn gets no deployment grant. A second reader
    of self.secrets or self.net_allowlist would be a second way to hand a
    tenant the operator's credential."""
    readers = set()
    for function in _functions(PI_ROOT / "agent.py"):
        for node in ast.walk(function):
            if (isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load)
                    and node.attr in ("secrets", "net_allowlist")
                    and isinstance(node.value, ast.Name) and node.value.id == "self"):
                readers.add(function.name)
    assert readers == {"_tool_grants"}, readers


def test_every_product_turn_passes_its_principals_grants():
    """Both turn paths (a chat request, a scheduled turn) hand the agent the
    grants resolved from the principal's policy for that very turn."""
    checked = 0
    for function in _functions(PI_ROOT / "product_service.py"):
        resolved = {target.id for node in ast.walk(function)
                    if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
                    and getattr(node.value.func, "attr",
                                getattr(node.value.func, "id", None))
                    in ("_tool_grants", "principal_tool_grants")
                    for target in node.targets if isinstance(target, ast.Name)}
        for node in ast.walk(function):
            if (isinstance(node, ast.Call)
                    and getattr(node.func, "attr", None) == "turn_with_usage"):
                spread = [k.value.id for k in node.keywords
                          if k.arg is None and isinstance(k.value, ast.Name)]
                assert len(spread) == 1 and spread[0] in resolved, (
                    f"{function.name} runs a turn without its principal's grants")
                checked += 1
    assert checked == 2, "the scan found no turn: it would pass vacuously"


def test_product_main_gives_the_agent_no_deployment_wide_grant():
    """The product's agent holds no deployment-wide secret or host: it is
    given an empty literal of each, so a principal's turn has nothing of the
    deployment's to fall back to even if that rule were lost."""
    calls = [node for node in ast.walk(ast.parse((PI_ROOT / "product_main.py").read_text()))
             if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "PiAgent"]
    assert len(calls) == 1
    given = {k.arg: k.value for k in calls[0].keywords if k.arg in ("secrets", "net_allowlist")}
    assert set(given) == {"secrets", "net_allowlist"}, given
    assert isinstance(given["secrets"], ast.Dict) and not given["secrets"].keys
    assert isinstance(given["net_allowlist"], ast.List) and not given["net_allowlist"].elts
