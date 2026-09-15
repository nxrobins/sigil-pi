"""Toolchain-free build recipe checks, NOT compiler or execution evidence."""

import hashlib

from emergency_support import ROOT, source


def test_emergency_recipe_is_deterministic_byte_bound_and_within_original_source_ceiling():
    built = source()
    assert built == source()
    assert 0 < len(built.text.encode()) <= 65536
    assert built.text.count("pub fn tool_main(") == 1
    assert len(built.input_hashes) == 8
    for name, digest in built.input_hashes.items():
        assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == digest


def test_policy_keeps_distinct_temporary_protocol_without_effect_or_durable_commit_imports():
    built = source()
    fragment = (ROOT / "app/pi/emergency_policy.sigil").read_text()
    assert "read_record(get(x, 5), \"VR1\\n\", 5)" in fragment
    assert "write_record(out, \"EA1\\n\", 4)" in fragment
    assert "readiness-window" in fragment
    assert 'return emergency_answer("admit",' in fragment
    assert "store_commit(" not in fragment
    assert "use sigil::net" not in built.text
    assert "use sigil::kv" not in built.text
    assert "use sigil::fs" not in built.text


def test_policy_reuses_the_same_sigil_guard_that_the_entry_can_call_before_reading():
    built = source()
    fragment = (ROOT / "app/pi/emergency_policy.sigil").read_text()
    assert fragment.count("// EMERGENCY_GUARDS") == 1
    assert "fn emergency_gate(" not in fragment
    assert "fn emergency_cause(" not in fragment
    assert "let gate: i64 @Internal = emergency_gate(" in fragment
    assert built.text.count("fn emergency_gate(") == 1
    assert built.text.count("fn emergency_cause(") == 1
