"""Build recipe guards; not actual SIGIL execution or host/API qualification."""
from types import SimpleNamespace
import hashlib

import pytest

import scripts.compose_http_entry as recipe
from conftest import PI_ROOT, SIGIL_ROOT
from scripts.compose_application import compose_application


def test_http_entry_fits_existing_source_limit_without_extra_entrypoints():
    built = recipe.compose_http_entry(PI_ROOT, SIGIL_ROOT)
    assert len(built.text.encode()) <= 65536
    assert built.text.count("pub fn tool_main(") == 1
    for name in ["api_response_core", "api_decision"]:
        assert f"fn {name}(input_ptr: i64 @Internal, input_len: i64 @Internal) -> i64 @Internal" in built.text
    assert '"AH5\\n", 15' in built.text and '"HC5\\n"' in built.text
    assert '"AH4\\n"' not in built.text and '"HC4\\n"' not in built.text
    assert {"app/pi/http_request_id.sigil", "app/pi/http_api.sigil", "scripts/compose_http_entry.py"} <= built.input_hashes.keys()
    for relative, digest in built.input_hashes.items():
        assert hashlib.sha256((PI_ROOT / relative).read_bytes()).hexdigest() == digest
    assert built.compiler_input_sha256 == "4261e3bcb64f0b7b6f80f5e0226336cc6ff5d3c020457b4af4724bd8f327e289"
    assert built.stdlib_hash == compose_application("api_discovery", SIGIL_ROOT).stdlib_hash


def test_existing_admitted_api_compiler_inputs_are_not_changed_by_new_recipe():
    assert compose_application("api", SIGIL_ROOT).compiler_input_sha256 == "95a7dbc7986cb1e51fc6613eef7e972096553807d7368ab1f72aff57813ec373"
    assert compose_application("api_discovery", SIGIL_ROOT).compiler_input_sha256 == recipe.BASELINE


def test_http_entry_recipe_refuses_an_unreviewed_api_baseline(monkeypatch):
    monkeypatch.setattr(recipe, "compose_application", lambda *a, **kw: SimpleNamespace(compiler_input_sha256="0" * 64))
    with pytest.raises(ValueError, match="baseline changed"):
        recipe.compose_http_entry(PI_ROOT, SIGIL_ROOT)
