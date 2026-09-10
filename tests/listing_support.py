"""Static v6 development deployment fixture; no application turn logic in Python."""
import hashlib
import json

from api_support import BODY, NativeApi, credential
from automatic_support import configuration
from conftest import FIXED_EVALUATOR_BIN, SIGIL_ROOT
from scripts.compose_application import compose_application
from turn_support import FUEL, record
from worker_support import runtime_digest

COMMANDS = ["call", "commit", "metadata", "read", "reply"]
FUNCTIONS = ["admission", "history", "listing"]
COMMAND_TEXT = json.dumps(COMMANDS, separators=(",", ":"))


def compose_entry():
    return compose_application("api_discovery", SIGIL_ROOT)


def compose_listing():
    return compose_application("listing", SIGIL_ROOT)


def envelope(*, method="GET", path="/v1/sessions", body="", row=None, now=150,
             operation="a" * 64, stage="init", observation="", continuation="", purpose=None,
             bundle="b" * 64, functions=None, commands=COMMAND_TEXT):
    return record("AH4\n", [method, path, json.dumps(BODY) if body is None else body,
        credential()["facts"] if row is None else row["facts"], str(now), operation,
        stage, observation, continuation, bundle,
        json.dumps(FUNCTIONS if functions is None else functions),
        purpose or ("boot" if stage == "boot" else "request"), commands])


def service(binary, root, programs, endpoint, workspace, *, rows=None, mode="init", patch=None):
    config = configuration(root, programs, endpoint, workspace, rows)

    def fixed(name, built):
        path = root / (name + ".sigil")
        path.write_text(built.text)
        return {"version": 1, "runtime": str(FIXED_EVALUATOR_BIN),
            "runtime_sha256": runtime_digest(str(FIXED_EVALUATOR_BIN)),
            "source": str(path), "source_sha256": hashlib.sha256(built.text.encode()).hexdigest(),
            "max_fuel": FUEL, "max_timeout_ms": 15000, "net": [], "fs": [], "secret_env": {}}

    config["version"] = 6
    # NativeApi writes the reference api.sigil; keep this admitted overlay separate.
    config["worker"] = fixed("listing-entry", compose_entry())
    config["functions"]["listing"] = fixed("listing", compose_listing())
    if patch:
        patch(config)
    return NativeApi(binary, root, config=config, mode=mode)
