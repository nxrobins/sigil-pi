"""The local runtime composer stays byte-identical to SIGIL's pinned helper."""

import sys

import pytest

from conftest import PI_ROOT, SIGIL_ROOT

from sigil_compose import compose_with_stdlib


def test_runtime_composer_matches_pinned_bench_composer():
    sys.path.insert(0, str(SIGIL_ROOT / "bench" / "src"))
    from sigil_bench.compose import compose_with_stdlib as upstream

    source = (PI_ROOT / "tools" / "agent_turn.sigil").read_text()
    ours = compose_with_stdlib(source, ["http"], SIGIL_ROOT)
    theirs = upstream(source, ["http"], SIGIL_ROOT)
    assert ours.text == theirs.text
    assert ours.stdlib_hash == theirs.stdlib_hash
    assert ours.modules_included == theirs.modules_included
    assert ours.stdlib_line_offset == theirs.stdlib_line_offset


def test_runtime_composer_rejects_path_traversal_before_file_io():
    try:
        compose_with_stdlib("module x;", ["../secret"], SIGIL_ROOT)
    except ValueError as error:
        assert "module name" in str(error)
    else:
        raise AssertionError("invalid module path was accepted")


def test_runtime_composer_handles_empty_and_missing_module_sets(tmp_path):
    empty = compose_with_stdlib("module x;", [], tmp_path)
    assert empty.text == "module x;" and empty.stdlib_hash == "0" * 24
    with pytest.raises(ValueError, match="not found"):
        compose_with_stdlib("module x;", ["missing"], tmp_path)
