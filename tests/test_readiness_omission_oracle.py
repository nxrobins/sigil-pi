"""Require the original native/compiler gates before the independent oracle."""

import os
from pathlib import Path
import shutil
import subprocess
import tomllib

import pytest

from conftest import PI_ROOT, SIGIL_ROOT
from scripts.readiness_entry_base import prepare_entry_base


ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def omission_oracle(native_fixed_evaluator_binary, tmp_path_factory):
    assert native_fixed_evaluator_binary.is_file()
    manifest = tomllib.loads((PI_ROOT / "native/evaluator/Cargo.toml").read_text())
    assert manifest["dependencies"]["sigil-compiler"]["rev"] == "8277a1d92d599df89e6b4391fc70fd0fa534d696"
    deps = native_fixed_evaluator_binary.parent / "deps"
    libraries = sorted(deps.glob("libsigil_compiler-*.rlib"))
    assert len(libraries) == 1, "refuse an ambiguous compiler oracle build"
    source = ROOT / "tests/fixtures/omission_oracle.rs"
    binary = tmp_path_factory.mktemp("readiness-omission-oracle") / "oracle"
    command = ["rustc", "--edition=2024", "-Dwarnings", "-O", str(source),
               "--extern", f"sigil_compiler={libraries[0]}", "-L", f"dependency={deps}", "-o", str(binary)]
    if shutil.which("brew"):
        found = subprocess.run(["brew", "--prefix", "z3"], capture_output=True, text=True, timeout=30, check=True)
        library = Path(found.stdout.strip()) / "lib"
        assert library.is_dir()
        command.extend(["-L", f"native={library}"])
    for args in (["rustfmt", "--check", "--edition", "2024", str(source)], command):
        result = subprocess.run(args, cwd=PI_ROOT / "native/evaluator", env=dict(os.environ),
                                capture_output=True, text=True, timeout=240)
        assert result.returncode == 0, result.stdout + result.stderr
    return binary


def compare(binary, root, before, after, *, layout_only=False):
    original, reduced = root / "original.sigil", root / "reduced.sigil"
    original.write_text(before)
    reduced.write_text(after)
    return subprocess.run([str(binary), str(original), str(reduced),
                           *(["--layout-only"] if layout_only else [])],
                          capture_output=True, text=True, timeout=30)


@pytest.mark.parametrize("limit", [2, 9223372036854775807])
def test_pinned_parser_confirms_whole_unused_definitions_and_all_retained_tokens(omission_oracle, tmp_path, limit):
    built = prepare_entry_base(PI_ROOT, SIGIL_ROOT, request_limit=limit)
    result = compare(omission_oracle, tmp_path, built.original, built.text)
    assert result.returncode == 0, result.stdout + result.stderr
    rows = [line.split() for line in result.stdout.splitlines() if line.startswith("removed ")]
    assert {name: (int(start), int(end)) for _, name, start, end in rows} == {
        item.name: (item.start, item.end) for item in built.omissions}
    assert "zero parser/lexer diagnostics" in result.stdout


@pytest.mark.parametrize("limit", [2, 9223372036854775807])
def test_pinned_lexer_preserves_every_token_with_operator_spacing_only(omission_oracle, tmp_path, limit):
    built = prepare_entry_base(PI_ROOT, SIGIL_ROOT, request_limit=limit)
    assert len(built.original.encode()) - len(built.layout_only.encode()) == 4898
    result = compare(omission_oracle, tmp_path, built.original, built.layout_only, layout_only=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "removed " not in result.stdout


def test_real_oracle_detects_a_changed_retained_string_literal(omission_oracle, tmp_path):
    built = prepare_entry_base(PI_ROOT, SIGIL_ROOT, request_limit=2)
    changed = built.text.replace('"request-window"', '"other-window"', 1)
    assert changed != built.text
    result = compare(omission_oracle, tmp_path, built.original, changed)
    assert result.returncode != 0 and "retained compiler token or literal changed" in result.stderr


def test_real_oracle_refuses_removal_of_a_now_referenced_function(omission_oracle, tmp_path):
    built = prepare_entry_base(PI_ROOT, SIGIL_ROOT, request_limit=2)
    suffix = "fn reference_probe() -> i64 { return action_key(0, 0); }"
    result = compare(omission_oracle, tmp_path, built.original + suffix, built.text + suffix)
    assert result.returncode != 0 and "still has a reference" in result.stderr
