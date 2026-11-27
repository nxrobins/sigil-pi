"""Rebuild a frozen, test-only older host; never choose product runtime behavior."""
import hashlib
import json
import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).parent / "fixtures/native-host-v4"
SNAPSHOT = "7a6c2838f514b67a34fef88e99674c0e123527365585a38b8d4bb8406b79b0d5"


def validate_snapshot():
    manifest = json.loads((ROOT / "manifest.json").read_text())
    assert manifest["schema_version"] == 1
    files = manifest["files"]
    assert len(files) == 34
    encoded = "".join(f"{files[name]}  {name}\n" for name in sorted(files)).encode()
    assert hashlib.sha256(encoded).hexdigest() == manifest["source_aggregate_sha256"] == SNAPSHOT
    actual = {path.relative_to(ROOT).as_posix() for path in ROOT.rglob("*")
              if path.is_file() and "target" not in path.relative_to(ROOT).parts
              and path.name not in {"README.md", "manifest.json"}}
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
    commands = [["cargo", "fmt", "--check"],
        ["cargo", "clippy", "--locked", "--all-targets", "--", "-D", "warnings"],
        ["cargo", "test", "--locked", "--quiet"], command]
    for action in commands:
        result = subprocess.run(action, cwd=root, capture_output=True, text=True,
            timeout=240, env={**os.environ, "CARGO_BUILD_JOBS": "2"})
        assert result.returncode == 0, f"legacy compatibility gate failed: {action}\n{result.stdout}\n{result.stderr}"
    return root / "target" / ("release" if release else "debug") / binary
