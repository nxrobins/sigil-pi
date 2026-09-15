"""Build/pin/bootstrap contract for the fixed grantless evaluator."""
import tomllib

from automatic_support import configuration
from conftest import FIXED_EVALUATOR_BIN, MCP_BIN, PI_ROOT
from test_turn_execution import programs as programs


def test_evaluator_compiler_and_runtime_follow_the_full_sigil_pin():
    pin = next(line.split("=", 1)[1].strip() for line in (PI_ROOT / "SIGIL_REV").read_text().splitlines()
               if line.startswith("ref ="))
    manifest = tomllib.loads((PI_ROOT / "native/evaluator/Cargo.toml").read_text())
    for name in ("sigil-compiler", "sigil-runtime"):
        entry = manifest["dependencies"][name]
        assert entry["git"] == "https://github.com/nxrobins/sigil" and entry["rev"] == pin
        assert "solver" in entry["features"]
    packages = tomllib.loads((PI_ROOT / "native/evaluator/Cargo.lock").read_text())["package"]
    expected = {"sigil-abi", "sigil-compiler", "sigil-formal-bridge", "sigil-runtime"}
    actual = {row["name"]: row["source"] for row in packages if row["name"] in expected}
    assert set(actual) == expected
    assert all(source == f"git+https://github.com/nxrobins/sigil?rev={pin}#{pin}" for source in actual.values())
    assert f'const SIGIL_PIN: &str = "{pin}";' in (PI_ROOT / "native/evaluator/src/main.rs").read_text()


def test_automatic_bootstrap_uses_fixed_code_only_for_grantless_cached_functions(
        native_fixed_evaluator_binary, programs, tmp_path):
    config = configuration(tmp_path / "config", programs, "http://127.0.0.1:1/messages", tmp_path)
    assert sorted(config["functions"]) == ["admission", "history"]
    rows = config["automatic"]["participants"]
    fixed = [config["worker"], *config["functions"].values(), *(row["worker"] for row in rows)]
    for worker in fixed:
        assert worker["runtime"] == str(FIXED_EVALUATOR_BIN) == str(native_fixed_evaluator_binary)
        assert worker["net"] == worker["fs"] == [] and worker["secret_env"] == {}
    for row in rows:
        for effect in row["effects"].values():
            assert effect["worker"]["runtime"] == str(MCP_BIN)
            assert effect["policy"]["worker"]["runtime"] == str(MCP_BIN)
            assert effect["recorder"]["worker"]["runtime"] == str(MCP_BIN)
        assert all(tx["worker"]["runtime"] == str(MCP_BIN) for tx in row["transactions"].values())
