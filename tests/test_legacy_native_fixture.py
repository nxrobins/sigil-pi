"""Older-host checks use actual frozen source, not an imitation version checker."""
import shutil

import pytest

import legacy_native_support
from legacy_native_support import validate_snapshot


def test_legacy_native_compatibility_source_is_frozen_and_complete():
    root = validate_snapshot()
    assert (root / "service/src/main.rs").is_file()
    assert (root / "store/src/main.rs").is_file()


@pytest.mark.parametrize("change", ["source", "lockfile", "extra_source"])
def test_legacy_fixture_refuses_drift_before_a_build(tmp_path, monkeypatch, change):
    root = tmp_path / "snapshot"
    shutil.copytree(legacy_native_support.ROOT, root, ignore=shutil.ignore_patterns("target"))
    monkeypatch.setattr(legacy_native_support, "ROOT", root)
    path = root / {"source": "store/src/main.rs", "lockfile": "store/Cargo.lock",
                   "extra_source": "service/build.rs"}[change]
    path.write_text((path.read_text() if path.exists() else "") + "\n// unreviewed fixture drift\n")
    with pytest.raises(AssertionError):
        validate_snapshot()
