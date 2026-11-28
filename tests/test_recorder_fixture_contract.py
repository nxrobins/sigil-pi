"""Compile the failure fixture's untouched path before trusting its fault slice."""
from pathlib import Path

import pytest

from completion_support import facts, snapshot
from conftest import PI_ROOT, SIGIL_ROOT, forge_ok
from scripts.compose_application import compose_application
from test_recorder_evaluation_audit import install_failure
from turn_support import FUEL, refused


@pytest.mark.parametrize("phase", ["prepared", "observed"])
@pytest.mark.parametrize("kind", ["runtime_error", "malformed"])
def test_failure_fixture_preserves_the_real_recorder_on_its_untouched_path(mcp, tmp_path, phase, kind):
    original = compose_application("worker_completion", SIGIL_ROOT, root=PI_ROOT).text
    path = tmp_path / "original.sigil"
    path.write_text(original)
    worker = {"source": str(path)}
    config = {"automatic": {"participants": [{"effect_audit": {}, "effects": {
        "provider": {"recorder": {"worker": worker}},
    }}]}}
    install_failure(config, tmp_path, phase=phase, kind=kind, enabled=False)
    modified = Path(worker["source"]).read_text()
    prepared = snapshot(facts(kind="prepared", sent="0", status="", output_kind="missing", payload=""), revision="0")
    observed = snapshot(facts())
    untouched, failing = (observed, prepared) if phase == "prepared" else (prepared, observed)
    assert forge_ok(mcp, modified, untouched, fuel=FUEL) == forge_ok(mcp, original, untouched, fuel=FUEL)
    if kind == "runtime_error":
        refused(mcp, modified, failing, 403)
    else:
        assert forge_ok(mcp, modified, failing, fuel=FUEL) == "private malformed recorder output"
