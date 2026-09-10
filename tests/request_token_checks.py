"""Pinned native-lexer checks, imported into the existing native-fixture module."""

import os
from pathlib import Path
import shutil
import subprocess
import tomllib

import pytest

from conftest import PI_ROOT
from request_entry_support import ROOT, compose_request_entry


@pytest.fixture(scope="session")
def request_lexer_oracle(native_fixed_evaluator_binary, tmp_path_factory):
    # The existing complete compiler/runtime native gate is a prerequisite.
    assert native_fixed_evaluator_binary.is_file()
    manifest = tomllib.loads((PI_ROOT / "native/evaluator/Cargo.toml").read_text())
    assert manifest["dependencies"]["sigil-compiler"]["rev"] == "8277a1d92d599df89e6b4391fc70fd0fa534d696"
    deps = native_fixed_evaluator_binary.parent / "deps"
    libraries = sorted(deps.glob("libsigil_compiler-*.rlib"))
    assert len(libraries) == 1, "refuse an ambiguous compiler oracle build"
    source = ROOT / "tests/fixtures/request_token_oracle.rs"
    binary = tmp_path_factory.mktemp("request-token-oracle") / "tokens"
    cwd = PI_ROOT / "native/evaluator"
    env = dict(os.environ)
    command = ["rustc", "--edition=2024", "-Dwarnings", "-O", str(source),
               "--extern", f"sigil_compiler={libraries[0]}", "-L", f"dependency={deps}", "-o", str(binary)]
    if shutil.which("brew"):
        found = subprocess.run(["brew", "--prefix", "z3"], capture_output=True, text=True, timeout=30, check=True)
        library = Path(found.stdout.strip()) / "lib"
        assert library.is_dir()
        command.extend(["-L", f"native={library}"])
    for args in (["rustfmt", "--check", "--edition", "2024", str(source)], command):
        result = subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True, timeout=240)
        assert result.returncode == 0, result.stdout + result.stderr
    return binary


def compare(binary, root, before, after):
    original, compact = root / "layout.sigil", root / "compact.sigil"
    original.write_text(before)
    compact.write_text(after)
    return subprocess.run([str(binary), str(original), str(compact)], capture_output=True, text=True, timeout=30)


@pytest.mark.parametrize("limit", [2, 9223372036854775807])
def test_full_layout_has_identical_real_compiler_tokens_and_literal_values(request_lexer_oracle, tmp_path, limit):
    built = compose_request_entry(limit)
    assert len(built.text.encode()) <= 65536 < len(built.layout_input.encode())
    result = compare(request_lexer_oracle, tmp_path, built.layout_input, built.text)
    assert result.returncode == 0, result.stderr
    count, suffix = result.stdout.strip().split(" ", 1)
    assert int(count) > 10000 and suffix == "identical tokens; zero lexer diagnostics"


def test_real_token_oracle_detects_a_changed_literal(request_lexer_oracle, tmp_path):
    original = 'module x; pub fn tool_main(p: i64,n: i64)->i64 @Internal { return 1; }'
    changed = original.replace("return 1", "return 2")
    assert compare(request_lexer_oracle, tmp_path, original, changed).returncode != 0
