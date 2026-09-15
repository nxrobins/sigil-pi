"""Freeze the real old v8 host before readiness changes; no runtime stub."""

import json
import shutil

import pytest

import legacy_readiness_native_support as legacy


def test_pre_readiness_native_compatibility_source_is_frozen_and_complete():
    root = legacy.validate_snapshot()
    for name in ("service/src/main.rs", "service/src/request_facts.rs",
                 "service/src/action/readset_tests.rs", "store/src/readset_tests.rs",
                 "worker/src/pure.rs"):
        assert (root / name).is_file()
    assert '"AH6\\n"' in (root / "service/src/action.rs").read_text()
    assert "pub mod volatile" not in (root / "store/src/lib.rs").read_text()
    assert not (root / "service/src/monotonic_facts.rs").exists()


@pytest.mark.parametrize("change", ["source", "lockfile", "extra_source", "missing",
                                    "symlink", "manifest", "manifest_symlink",
                                    "manifest_schema", "manifest_field"])
def test_pre_readiness_fixture_refuses_drift_before_a_build(tmp_path, monkeypatch, change):
    root = tmp_path / "snapshot"
    shutil.copytree(legacy.ROOT, root, ignore=shutil.ignore_patterns("target"))
    monkeypatch.setattr(legacy, "ROOT", root)
    path = root / {"source": "service/src/main.rs", "lockfile": "service/Cargo.lock",
                   "extra_source": "service/build.rs", "missing": "worker/src/pure.rs",
                   "symlink": "service/linked.rs", "manifest": "manifest.json",
                   "manifest_symlink": "manifest.json", "manifest_schema": "manifest.json",
                   "manifest_field": "manifest.json"}[change]
    if change == "missing":
        path.unlink()
    elif change == "symlink":
        path.symlink_to(root / "service/src/main.rs")
    elif change == "manifest_symlink":
        copy = tmp_path / "linked-manifest.json"
        copy.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(copy)
    elif change.startswith("manifest"):
        doc = json.loads(path.read_text())
        if change == "manifest_schema":
            doc["schema_version"] = True
        elif change == "manifest_field":
            doc["unreviewed"] = True
        else:
            doc["source_aggregate_sha256"] = "0" * 64
        path.write_text(json.dumps(doc))
    else:
        path.write_text((path.read_text() if path.exists() else "") + "\n// unreviewed drift\n")
    with pytest.raises(AssertionError):
        legacy.validate_snapshot()


def test_pre_readiness_fixture_refuses_a_symlinked_root(tmp_path, monkeypatch):
    link = tmp_path / "snapshot"
    link.symlink_to(legacy.ROOT, target_is_directory=True)
    monkeypatch.setattr(legacy, "ROOT", link)
    with pytest.raises(AssertionError):
        legacy.validate_snapshot()
