"""Static fixture checks only; never stand in for native or SIGIL execution."""
from browser_support import configure as configure_v6
from scripts.compose_http_entry import HEADER_NAMES
from http_service_support import configure, host_probe_source
from test_turn_execution import programs as programs


def test_http_static_deployment_keeps_all_v6_limits_grants_and_effect_bindings(programs, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = tmp_path / "service"
    # This reserved loopback URL is only serialized; no service is contacted.
    before, _ = configure_v6(root, programs, "http://127.0.0.1:1", workspace)
    after, _ = configure(root, programs, "http://127.0.0.1:1", workspace)
    assert after["version"] == 7
    assert after["http"] == {"response_headers": HEADER_NAMES}
    for key in before.keys() - {"version", "worker"}:
        assert before[key] == after[key], key
    for key in before["worker"].keys() - {"source", "source_sha256"}:
        assert before["worker"][key] == after["worker"][key], key
    assert set(after) - set(before) == {"http"}
    assert after["worker"]["max_timeout_ms"] == 15000
    assert after["worker"]["net"] == after["worker"]["fs"] == []
    assert after["worker"]["secret_env"] == {}


def test_explicit_host_probe_fits_same_source_limit_and_contains_real_bootstrap():
    source = host_probe_source()
    assert len(source.encode()) <= 65536
    assert source.count("pub fn tool_main(") == 1
    assert "fn http_probe_original(input_ptr: i64 @Internal, input_len: i64 @Internal) -> i64 @Internal" in source
    assert 'return http_probe_original(input_ptr, input_len)' in source
    assert '"AH5\\n", 15' in source and '"HC5\\n"' in source


def test_v7_browser_selection_keeps_every_original_test_function_and_parameter():
    import test_http_browser_regression as regression
    original = {name: value for module in regression.BASE_MODULES
                for name, value in vars(module).items() if name.startswith("test_") and callable(value)}
    selected = {name: value for name, value in vars(regression).items()
                if name.startswith("test_") and callable(value)}
    assert len(original) == 11
    assert selected == original
    for name in selected:
        assert selected[name] is original[name]
