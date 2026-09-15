"""Rebuild the frozen pre-HTTP-metadata v6 host for compatibility evidence only."""
import hashlib
import json
import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).parent / "fixtures/native-host-v6"
SNAPSHOT = "afb5134fe14255f55c80df446697de2e3e5d664ec6c7c0170a8e930f50ff3e2f"


def validate_snapshot():
    manifest = json.loads((ROOT / "manifest.json").read_text())
    assert manifest["schema_version"] == 1
    files = manifest["files"]
    assert len(files) == 36
    encoded = "".join(f"{files[name]}  {name}\n" for name in sorted(files)).encode()
    assert hashlib.sha256(encoded).hexdigest() == manifest["source_aggregate_sha256"] == SNAPSHOT
    actual = set()
    for path in ROOT.rglob("*"):
        relative = path.relative_to(ROOT)
        if "target" in relative.parts:
            continue
        assert not path.is_symlink(), "frozen compatibility input cannot be a symlink"
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
    for action in [["cargo", "fmt", "--check"],
                   ["cargo", "clippy", "--locked", "--all-targets", "--", "-D", "warnings"],
                   ["cargo", "test", "--locked", "--quiet"], command]:
        result = subprocess.run(action, cwd=root, capture_output=True, text=True,
            timeout=240, env={**os.environ, "CARGO_BUILD_JOBS": "2"})
        assert result.returncode == 0, f"frozen v6 compatibility gate failed: {action}\n{result.stdout}\n{result.stderr}"
    executable = root / "target" / ("release" if release else "debug") / binary
    assert executable.is_file(), f"missing frozen compatibility executable: {binary}"
    return executable
