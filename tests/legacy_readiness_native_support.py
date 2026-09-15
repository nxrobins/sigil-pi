"""Build the actual frozen pre-readiness v8 host for compatibility tests."""

import hashlib
import json
import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).parent / "fixtures/native-host-v8"
SNAPSHOT = "b2ddb658d807c3bf0b826baf235e73004200633edb6c196d3ba985177b56d639"


def validate_snapshot():
    assert ROOT.is_dir() and not ROOT.is_symlink(), "frozen root must be a real directory"
    manifest_path = ROOT / "manifest.json"
    assert manifest_path.is_file() and not manifest_path.is_symlink()
    manifest = json.loads(manifest_path.read_text())
    assert set(manifest) == {"schema_version", "source_aggregate_sha256", "files"}
    assert type(manifest["schema_version"]) is int and manifest["schema_version"] == 1
    files = manifest["files"]
    assert type(files) is dict and len(files) == 46
    encoded = "".join(f"{files[name]}  {name}\n" for name in sorted(files)).encode()
    assert hashlib.sha256(encoded).hexdigest() == manifest["source_aggregate_sha256"] == SNAPSHOT
    actual = set()
    for path in ROOT.rglob("*"):
        relative = path.relative_to(ROOT)
        if "target" in relative.parts:
            continue
        assert not path.is_symlink(), "frozen compatibility input cannot be a symlink"
        assert path.is_file() or path.is_dir(), "frozen input must be a regular file or directory"
        if path.is_file() and relative.as_posix() not in {"README.md", "manifest.json"}:
            actual.add(relative.as_posix())
    assert actual == set(files), "frozen compatibility source inventory changed"
    for name, expected in files.items():
        relative = Path(name)
        assert not relative.is_absolute() and ".." not in relative.parts
        assert hashlib.sha256((ROOT / relative).read_bytes()).hexdigest() == expected, name
    return ROOT


def build(crate, binary, *, release=False):
    assert crate in {"store", "worker", "service"}
    root = validate_snapshot() / crate
    command = ["cargo", "build", "--locked", "--bins"]
    if release:
        command.insert(2, "--release")
    environment = {**os.environ, "CARGO_BUILD_JOBS": "2"}
    environment.pop("CARGO_TARGET_DIR", None)
    for action in [["cargo", "fmt", "--check"],
                   ["cargo", "clippy", "--locked", "--all-targets", "--", "-D", "warnings"],
                   ["cargo", "test", "--locked", "--quiet"], command]:
        result = subprocess.run(action, cwd=root, capture_output=True, text=True,
                                timeout=240, env=environment)
        assert result.returncode == 0, f"frozen v8 gate failed: {action}\n{result.stdout}\n{result.stderr}"
    executable = root / "target" / ("release" if release else "debug") / binary
    assert executable.is_file(), f"missing frozen compatibility executable: {binary}"
    return executable
