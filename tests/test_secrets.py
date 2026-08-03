"""M14a — the general secret grant: `{SECRET:name}`.

M12 shipped `{GITHUB_TOKEN}`, which was right for one provider and calcifies
at three: a new env var, a new constructor parameter, and a new branch in the
grant resolver per API. This replaces it with one form that reads
`PI_SECRET_<NAME>` from the operator's environment.

The security properties are the ones M12 established and must not regress:
the value never enters a guest (it goes to the runtime as a grant, and the
guest names only a `{{secret:NAME}}` placeholder), and an unconfigured secret
expands EMPTY so the placeholder is ungranted and the runtime refuses with
-403 before any request leaves — the same fail-closed shape as
`{NET_ALLOWLIST}`.
"""
import json

import pytest

from conftest import API_KEY, msg, text, tool_use  # noqa: F401  (fixtures)
from test_pipeline import _spec


def _agent(scripted_llm, tmp_path, mcp, manifest, secrets=None):
    from agent import AuditLog, PiAgent, SessionStore
    (tmp_path / "sessions").mkdir(exist_ok=True)
    (tmp_path / "sandboxes").mkdir(exist_ok=True)
    mpath = tmp_path / "manifest.json"
    mpath.write_text(json.dumps(manifest))
    return PiAgent(scripted_llm.url, API_KEY,
                   store=SessionStore(tmp_path / "sessions"),
                   sandbox_root=tmp_path / "sandboxes", mcp=mcp,
                   model="claude-mock", manifest_path=mpath,
                   secrets=secrets or {},
                   audit=AuditLog(tmp_path / "audit", enabled=False))


# ── parsing the operator's environment ───────────────────────────────────


def test_secrets_from_env_collects_every_pi_secret(monkeypatch):
    """One convention, any number of providers: PI_SECRET_<NAME> becomes the
    secret named <name>, lowercased so the manifest reads `{SECRET:github}`
    rather than shouting."""
    from agent import secrets_from_env
    monkeypatch.setenv("PI_SECRET_GITHUB", "ghp_x")
    monkeypatch.setenv("PI_SECRET_NOTION", "ntn_y")
    monkeypatch.setenv("PI_SECRET_", "no-name")          # malformed, ignored
    monkeypatch.setenv("PI_SECRET_EMPTY", "")            # set-but-empty is unset
    monkeypatch.setenv("PI_NOT_A_SECRET", "z")
    out = secrets_from_env()
    assert out == {"github": "ghp_x", "notion": "ntn_y"}


def test_secrets_from_env_is_empty_when_nothing_is_configured(monkeypatch):
    for k in list(__import__("os").environ):
        if k.startswith("PI_SECRET_"):
            monkeypatch.delenv(k, raising=False)
    from agent import secrets_from_env
    assert secrets_from_env() == {}


# ── grant expansion ──────────────────────────────────────────────────────


ECHO = {
    "peek": {
        "source": "tests/fixtures/echo_tool.sigil",
        "args": ["v"], "path_args": [],
        "grants": {"secret": ["{SECRET:github}"]},
        "spec": _spec("peek", ["v"]),
    }
}


def test_configured_secret_expands_to_a_named_grant(scripted_llm, tmp_path, mcp):
    agent = _agent(scripted_llm, tmp_path, mcp, ECHO, {"github": "ghp_REAL"})
    scripted_llm.script = [msg([tool_use("t", "peek", {"v": "x"})]),
                           msg([text("ok")])]
    agent.turn("s1", "go")
    assert agent.grant_log[0] == ("peek", {"secret": ["github=ghp_REAL"]})


def test_unconfigured_secret_expands_empty_and_fails_closed(scripted_llm, tmp_path, mcp):
    """The M12 guarantee, generalized: no configured secret means an empty
    grant, so a guest naming the placeholder is refused by the runtime rather
    than proceeding unauthenticated. The key SURVIVES empty (never vanishes),
    matching {NET_ALLOWLIST}."""
    agent = _agent(scripted_llm, tmp_path, mcp, ECHO, {})
    scripted_llm.script = [msg([tool_use("t", "peek", {"v": "x"})]),
                           msg([text("ok")])]
    agent.turn("s1", "go")
    assert agent.grant_log[0] == ("peek", {"secret": []})


def test_several_providers_coexist(scripted_llm, tmp_path, mcp):
    """The whole point of generalizing: two providers, no new code path."""
    manifest = {"multi": {**ECHO["peek"],
                          "grants": {"secret": ["{SECRET:github}",
                                                "{SECRET:notion}"]},
                          "spec": _spec("multi", ["v"])}}
    agent = _agent(scripted_llm, tmp_path, mcp, manifest,
                   {"github": "g", "notion": "n", "unused": "u"})
    scripted_llm.script = [msg([tool_use("t", "multi", {"v": "x"})]),
                           msg([text("ok")])]
    agent.turn("s1", "go")
    _, grants = agent.grant_log[0]
    assert grants == {"secret": ["github=g", "notion=n"]}, \
        "only the secrets a tool NAMES may be granted to it"


def test_a_tool_gets_only_the_secrets_it_names(scripted_llm, tmp_path, mcp):
    """Minimality: configuring three secrets must not hand all three to every
    tool. This is the manifest-minimality thesis applied to credentials."""
    agent = _agent(scripted_llm, tmp_path, mcp, ECHO,
                   {"github": "g", "notion": "n", "slack": "s"})
    scripted_llm.script = [msg([tool_use("t", "peek", {"v": "x"})]),
                           msg([text("ok")])]
    agent.turn("s1", "go")
    _, grants = agent.grant_log[0]
    assert grants == {"secret": ["github=g"]}
    assert "notion=n" not in json.dumps(grants)


@pytest.mark.parametrize("token", ["{SECRET:}", "{SECRET:a b}", "{SECRET:a=b}"])
def test_malformed_secret_tokens_are_rejected_at_construction(
        scripted_llm, tmp_path, mcp, token):
    """A malformed token must fail LOUDLY at construction, not silently
    expand to nothing — a typo that reads as 'no secret configured' would be
    indistinguishable from an intentional fail-closed deployment."""
    manifest = {"bad": {**ECHO["peek"], "grants": {"secret": [token]},
                        "spec": _spec("bad", ["v"])}}
    with pytest.raises(ValueError, match="SECRET"):
        _agent(scripted_llm, tmp_path, mcp, manifest, {"github": "g"})


def test_secret_values_never_reach_the_model(scripted_llm, tmp_path, mcp):
    """The spec the model sees must never carry a credential — it is sent to
    the LLM on every request."""
    agent = _agent(scripted_llm, tmp_path, mcp, ECHO, {"github": "ghp_REAL"})
    scripted_llm.script = [msg([text("hi")])]
    agent.turn("s1", "go")
    assert "ghp_REAL" not in json.dumps(scripted_llm.requests[0])
