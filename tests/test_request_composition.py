"""Production build recipe integrity; not runtime or readiness qualification."""

import hashlib
from types import SimpleNamespace

import pytest

from conftest import PI_ROOT, SIGIL_ROOT
from scripts.compose_http_entry import compose_http_entry
import scripts.compose_request_entry as entry
from scripts.compose_request_policy import compose_request_policy


def test_integrated_build_recipes_preserve_the_verified_inputs_without_test_imports():
    built = entry.compose_request_entry(PI_ROOT, SIGIL_ROOT, request_limit=2)
    policy = compose_request_policy(PI_ROOT, SIGIL_ROOT)
    assert len(built.text.encode()) == 63349
    assert built.compiler_input_sha256 == "1d32fdafea96a37900b9302b6ba4fdbecb56019dc2bd7c98bee6dd82e9591824"
    assert len(policy.text.encode()) == 37066
    assert policy.compiler_input_sha256 == "0209e73f45b4911ef400474094c6768389810d9260feab569408bb3dd4fd7c9f"
    assert len(built.input_hashes) == 20 and len(policy.input_hashes) == 8
    for program in (built, policy):
        for name, digest in program.input_hashes.items():
            assert not name.startswith("tests/")
            if name == "configuration/request-limit":
                assert digest == hashlib.sha256(b"2").hexdigest()
            else:
                assert hashlib.sha256((PI_ROOT / name).read_bytes()).hexdigest() == digest
    assert built.stdlib_hash == policy.stdlib_hash == "b5f40e2eba41b6734f8db071"
    assert compose_http_entry(PI_ROOT, SIGIL_ROOT).compiler_input_sha256 == entry.BASELINE


def test_production_entry_requires_an_explicit_owner_build_limit():
    with pytest.raises(TypeError, match="request_limit"):
        entry.compose_request_entry(PI_ROOT, SIGIL_ROOT)


def test_production_entry_refuses_unreviewed_v7_baseline(monkeypatch):
    monkeypatch.setattr(entry, "compose_http_entry",
                        lambda *a, **kw: SimpleNamespace(compiler_input_sha256="0" * 64))
    with pytest.raises(ValueError, match="reviewed v7 source fingerprint"):
        entry.compose_request_entry(PI_ROOT, SIGIL_ROOT, request_limit=2)
