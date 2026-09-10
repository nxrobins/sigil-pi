"""The HTTP upgrade compares actual frozen v6 source, not a version-check stub."""
import shutil

import pytest

import legacy_http_native_support as legacy


def test_pre_http_native_compatibility_source_is_frozen_and_complete():
    root = legacy.validate_snapshot()
    assert (root / "service/src/main.rs").is_file()
    assert (root / "service/src/public_assets.rs").is_file()
    assert (root / "worker/src/pure.rs").is_file()


@pytest.mark.parametrize("change", ["source", "lockfile", "extra_source", "missing", "symlink"])
def test_pre_http_fixture_refuses_drift_before_a_build(tmp_path, monkeypatch, change):
    root = tmp_path / "snapshot"
    shutil.copytree(legacy.ROOT, root, ignore=shutil.ignore_patterns("target"))
    monkeypatch.setattr(legacy, "ROOT", root)
    path = root / {"source": "service/src/main.rs", "lockfile": "service/Cargo.lock",
                   "extra_source": "service/build.rs", "missing": "worker/src/pure.rs",
                   "symlink": "service/linked.rs"}[change]
    if change == "missing":
        path.unlink()
    elif change == "symlink":
        path.symlink_to(root / "service/src/main.rs")
    else:
        path.write_text((path.read_text() if path.exists() else "") + "\n// unreviewed fixture drift\n")
    with pytest.raises(AssertionError):
        legacy.validate_snapshot()
