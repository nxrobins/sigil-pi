"""Executable legacy API inventory, not migrated SIGIL product qualification.

This oracle stays in Python deliberately. The new SIGIL service must reproduce
the supported contract; passing these checks alone says nothing about that parity.
"""

import ast
import hashlib
import inspect
import json
import textwrap

import pytest

from conftest import PI_ROOT
from product_service import AuthRegistry, ProductService, _internal_session


INVENTORY = json.loads((PI_ROOT / "config/api-migration.json").read_text())
ROUTES = INVENTORY["legacy_routes"]
TOKEN = "migration-oracle-inert-test-credential-with-no-external-authority"


class OracleAgent:
    manifest = {"read_file": {}}
    memory = None
    _mcp = object()

    def __init__(self):
        self.calls = []

    def turn_with_usage(self, session, message, *, allowed_tools, deadline_monotonic):
        self.calls.append((session, message, allowed_tools, deadline_monotonic))
        return "fixture answer", {"input_tokens": 3, "output_tokens": 2}


def oracle(scopes):
    registry = AuthRegistry([{
        "sha256": hashlib.sha256(TOKEN.encode()).hexdigest(),
        "principal": "fixture-principal",
        "tenant": "fixture-tenant",
        "scopes": scopes,
        "tools": ["read_file"],
        "not_before_unix": 100,
        "expires_unix": 200,
    }], clock=lambda: 150, require_expiry=True)
    agent = OracleAgent()
    records = []
    service = ProductService(agent, registry, version="fixture-version",
                             log_sink=records.append)
    return service, agent, records


def route_id(row):
    return f"{row['method']} {row['path']}"


def test_legacy_inventory_matches_every_dispatch_branch_and_scope():
    """An omitted new route/method/scope fails before a migration can hide it."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(ProductService.dispatch)))
    actual = []
    for branch in ast.walk(tree):
        if not isinstance(branch, ast.If) or not branch.body:
            continue
        first = branch.body[0]
        if not (isinstance(first, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "route"
                        for t in first.targets)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
                and first.value.value.startswith("/v1/")):
            continue
        methods = [node.comparators[0].value for node in ast.walk(branch.test)
                   if isinstance(node, ast.Compare)
                   and isinstance(node.left, ast.Name) and node.left.id == "method"
                   and len(node.ops) == 1 and isinstance(node.ops[0], ast.Eq)
                   and isinstance(node.comparators[0], ast.Constant)]
        scopes = [stmt.value.args[1].value for stmt in branch.body
                  if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)
                  and isinstance(stmt.value.func, ast.Attribute)
                  and stmt.value.func.attr == "_require"]
        assert len(methods) == len(scopes) == 1, ast.dump(branch.test)
        actual.append((methods[0], first.value.value, scopes[0]))
    claimed = [(row["method"], row["path"], row["scope"]) for row in ROUTES]
    assert len(actual) == 11, "Deliberately review changes to the baseline inventory"
    assert len(claimed) == len(set(claimed)), "Duplicate inventory row"
    assert sorted(actual) == sorted(claimed)
    assert INVENTORY["schema_version"] == 1
    assert INVENTORY["status"] == "draft"
    planned = [(row["method"], row["path"]) for row in INVENTORY["planned_routes"]]
    assert len(planned) == len(set(planned)) == 5
    assert not set(planned) & {(method, path) for method, path, _ in claimed}


@pytest.mark.parametrize("row", ROUTES, ids=route_id)
@pytest.mark.parametrize("credential,expected,error", [
    (None, 401, "authentication_required"),
    ("unknown-fixture-credential", 401, "invalid_credential"),
    (TOKEN, 403, "permission_denied"),
])
def test_each_legacy_route_denies_before_work(row, credential, expected, error):
    service, agent, records = oracle([])
    headers = {"X-Request-ID": "migration-oracle"}
    if credential is not None:
        headers["Authorization"] = f"Bearer {credential}"
    body = b"" if row["body"] is None else json.dumps(row["body"]).encode()
    status, response_headers, payload = service.dispatch(
        row["method"], row["sample_path"], headers, body)
    assert status == expected
    assert payload["error"]["code"] == error
    assert set(payload) == {"error", "request_id"}
    assert set(payload["error"]) == {"code", "message"}
    assert payload["request_id"] == "migration-oracle"
    assert response_headers["Cache-Control"] == "no-store"
    assert response_headers["X-Request-ID"] == "migration-oracle"
    if expected == 401:
        assert response_headers["WWW-Authenticate"] == 'Bearer realm="sigil-pi"'
    assert not agent.calls
    assert len(records) == 1
    serialized = json.dumps([records, payload])
    assert TOKEN not in serialized
    assert "Read the permitted files." not in serialized


def test_legacy_chat_remains_synchronous_and_credential_scoped():
    service, agent, _ = oracle(["chat"])
    row = next(row for row in ROUTES if row["path"] == "/v1/chat")
    status, _, payload = service.dispatch(
        "POST", "/v1/chat", {"Authorization": f"Bearer {TOKEN}",
                               "X-Request-ID": "migration-oracle"},
        json.dumps(row["body"]).encode())
    assert status == 200
    assert payload == {
        "request_id": "migration-oracle", "session": "migration-fixture",
        "reply": "fixture answer", "usage": {"input_tokens": 3, "output_tokens": 2},
    }
    assert len(agent.calls) == 1
    session, message, tools, deadline = agent.calls[0]
    assert session == _internal_session("fixture-tenant", "migration-fixture")
    assert message == row["body"]["message"]
    assert tools == {"read_file"}
    assert deadline > 0


def test_legacy_chat_cannot_accept_planned_operation_fields_silently():
    service, agent, _ = oracle(["chat"])
    status, _, payload = service.dispatch(
        "POST", "/v1/chat", {"Authorization": f"Bearer {TOKEN}"},
        json.dumps({"session": "fixture", "message": "hello",
                    "submission_key": "not-a-v1-chat-field"}).encode())
    assert status == 400
    assert payload["error"]["code"] == "invalid_request"
    assert not agent.calls
