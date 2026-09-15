"""Request admission compares real frozen v7 sources, never a rejection stub."""
import shutil

import pytest

import legacy_request_native_support as legacy


def test_pre_request_native_compatibility_source_is_frozen_and_complete():
    root = legacy.validate_snapshot()
    for name in ("service/src/main.rs", "service/src/http_exchange.rs",
                 "service/src/action/http_tests.rs", "worker/src/pure.rs"):
        assert (root / name).is_file()


@pytest.mark.parametrize("change", ["source", "lockfile", "extra_source", "missing", "symlink", "manifest"])
def test_pre_request_fixture_refuses_drift_before_a_build(tmp_path, monkeypatch, change):
    root = tmp_path / "snapshot"
    shutil.copytree(legacy.ROOT, root, ignore=shutil.ignore_patterns("target"))
    monkeypatch.setattr(legacy, "ROOT", root)
    path = root / {"source": "service/src/main.rs", "lockfile": "service/Cargo.lock",
                   "extra_source": "service/build.rs", "missing": "worker/src/pure.rs",
                   "symlink": "service/linked.rs", "manifest": "manifest.json"}[change]
    if change == "missing":
        path.unlink()
    elif change == "symlink":
        path.symlink_to(root / "service/src/main.rs")
    elif change == "manifest":
        path.write_text(path.read_text().replace(legacy.SNAPSHOT, "0" * 64))
    else:
        path.write_text((path.read_text() if path.exists() else "") + "\n// unreviewed fixture drift\n")
    with pytest.raises(AssertionError):
        legacy.validate_snapshot()
