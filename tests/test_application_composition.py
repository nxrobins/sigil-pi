"""The build recipe must preserve code and report every authored input."""

import hashlib

import pytest

from conftest import PI_ROOT, SIGIL_ROOT, needs_toolchain
from scripts.compose_application import compose_application, strip_line_comments


@pytest.mark.parametrize("source,expected", [
    ("x // comment\ny", "x  \ny"),
    ('"https://example.test/a" // comment', '"https://example.test/a"  '),
    ('"escaped \\\" // still text" // removed\n', '"escaped \\\" // still text"  \n'),
    ("'/' // comment", "'/'  "),
    ('"multi\n// string\nline"', '"multi\n// string\nline"'),
    ('/* " // in block */ x // tail', '/* " // in block */ x  '),
    ('/* nested /* // inner */ outer */ // tail', '/* nested /* // inner */ outer */  '),
    ('"unfinished\\', '"unfinished\\'),
    ('/* unfinished // block', '/* unfinished // block'),
    ("", ""),
])
def test_comment_removal_preserves_literals_and_block_comments(source, expected):
    assert strip_line_comments(source) == expected


def test_unknown_component_cannot_select_an_arbitrary_path():
    with pytest.raises(ValueError, match="unknown application component"):
        compose_application("../secrets", "/not-used")


def test_discovery_recipe_preserves_the_qualified_programs_and_legacy_api():
    needs_toolchain()
    expected = {
        "api": "95a7dbc7986cb1e51fc6613eef7e972096553807d7368ab1f72aff57813ec373",
        "api_discovery": "ec1d1bf060609fc0a166d49325e76f3f63b5e45070a24faa5a2d72474150d34b",
        "listing": "9107a84e5fad6a497730af2b0bfb3327b49e126b29e65983f06452625f629ee6",
    }
    for name, digest in expected.items():
        assert compose_application(name, SIGIL_ROOT).compiler_input_sha256 == digest


def test_discovery_recipe_refuses_to_rebase_a_changed_core_silently(tmp_path):
    needs_toolchain()
    base = compose_application("api", SIGIL_ROOT)
    for name in base.input_hashes:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((PI_ROOT / name).read_bytes())
    api = tmp_path / "app/pi/api.sigil"
    api.write_text(api.read_text() + '\nfn unreviewed_contract_change() -> i64 @Internal { return 1; }\n')
    with pytest.raises(ValueError, match="baseline changed"):
        compose_application("api_discovery", SIGIL_ROOT, root=tmp_path)


@pytest.mark.parametrize("name,count", [("delivery_state", 1), ("submission", 2), ("turn", 4),
                                       ("read_request", 4), ("turn_transaction", 6), ("turn_completion", 7), ("executor_transaction", 5), ("worker_completion", 6), ("api", 9), ("api_discovery", 11), ("admission", 8), ("history", 7), ("listing", 5), ("dispatch", 11), ("settlement", 10), ("coordinator", 9), ("preclaim", 12), ("dispatch_cancellable", 13), ("preclaim_cancellable", 14)])
def test_recipe_hashes_all_authored_inputs_and_fits_unchanged_forge_limit(name, count):
    needs_toolchain()
    built = compose_application(name, SIGIL_ROOT)
    assert len(built.input_hashes) == count
    for path, digest in built.input_hashes.items():
        assert hashlib.sha256((PI_ROOT / path).read_bytes()).hexdigest() == digest
    assert built.compiler_input_sha256 == hashlib.sha256(built.text.encode()).hexdigest()
    assert 0 < len(built.text.encode()) <= 65536
    assert built == compose_application(name, SIGIL_ROOT)
    assert "// UTF8_VALIDATOR" not in built.text and "// TURN_HELPERS" not in built.text
    assert "// TURN_ENTRYPOINT" not in built.text
    assert "// RECORD_HELPERS" not in built.text and "// STORE_PROTOCOL" not in built.text


def test_preclaim_and_dispatch_use_exactly_one_identical_allowance_implementation():
    needs_toolchain()
    digest = hashlib.sha256((PI_ROOT / "app/pi/model_allowance.sigil").read_bytes()).hexdigest()
    for name in ["preclaim", "dispatch", "preclaim_cancellable", "dispatch_cancellable"]:
        result = compose_application(name, SIGIL_ROOT)
        assert result.input_hashes["app/pi/model_allowance.sigil"] == digest
        assert result.text.count("fn model_allowance(") == 1


def test_missing_composition_marker_fails_instead_of_omitting_code(tmp_path):
    app = tmp_path / "app/pi"
    app.mkdir(parents=True)
    (app / "turn.sigil").write_text("module pi_turn;\n")
    (app / "turn_helpers.sigil").write_text("// fixture helper\n")
    with pytest.raises(ValueError, match="expected exactly one composition marker"):
        compose_application("turn", "/not-used", root=tmp_path)


@pytest.mark.parametrize("source,expected", [
    ("  fn a() {\n\t return 1;\n  }", "fn a() {\nreturn 1;\n}"),
    ('  "first\n  literal\n\tstill literal"\n  next', '"first\n  literal\n\tstill literal"\nnext'),
    ('  /* first\n  inside block */\n  next', '/* first\n  inside block */\nnext'),
    ("  ' '\n  x", "' '\nx"),
    ("  // comment\n    x", " \nx"),
    ("x  +  y\n  z", "x  +  y\nz"),
])
def test_optional_indentation_compaction_preserves_tokens_newlines_and_literal_bytes(source, expected):
    assert strip_line_comments(source, compact_indent=True) == expected
