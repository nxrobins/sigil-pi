#!/usr/bin/env python3
"""Build a deterministic sigil-pi product release bundle."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import platform
import shutil
import stat
import subprocess
import tarfile
import tempfile
import uuid
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP_FILES = (
    "agent.py",
    "product_main.py",
    "product_service.py",
    "runtime_client.py",
    "sigil_compose.py",
    "state_tool.py",
    "toolchain.py",
    "VERSION",
    "requirements-runtime.lock",
    "scripts/release_drill.py",
)
DOC_FILES = (
    "README.md",
    "CHANGELOG.md",
    "SECURITY.md",
    "docs/api.md",
    "docs/capacity.md",
    "docs/operations.md",
    "docs/product-readiness.md",
    "docs/runbooks.md",
    "docs/security-guarantee.md",
    "docs/security/threat-model.md",
    "docs/state-compatibility.md",
    "docs/support-matrix.md",
)
STDLIB_MODULES = ("http", "json", "kv")


class ReleaseBuildError(RuntimeError):
    pass


def _pin(path):
    values = {}
    for line in Path(path).read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    missing = [key for key in ("crates", "stdlib", "ref") if not values.get(key)]
    if missing:
        raise ReleaseBuildError(f"pin file is missing: {missing}")
    return values


def _git(root, *args):
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
    if result.returncode:
        raise ReleaseBuildError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout.strip()


def verify_sigil_source(sigil_root, pin):
    sigil_root = Path(sigil_root)
    for area in ("crates", "stdlib"):
        actual = _git(sigil_root, "rev-parse", f"HEAD:{area}")
        if actual != pin[area]:
            raise ReleaseBuildError(
                f"SIGIL {area} tree does not match release pin: "
                f"expected {pin[area]}, got {actual}")
    dirty = _git(sigil_root, "status", "--porcelain", "--", "crates", "stdlib")
    if dirty:
        raise ReleaseBuildError("SIGIL crates/stdlib contain uncommitted changes")


def _copy_file(source, destination, executable=False):
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    destination.chmod(0o755 if executable else 0o644)


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sbom(version, pin):
    serial_seed = f"sigil-pi:{version}:{pin['ref']}"
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "serialNumber": f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, serial_seed)}",
        "version": 1,
        "metadata": {"component": {
            "type": "application", "name": "sigil-pi", "version": version,
        }},
        "components": [
            {"type": "application", "name": "sigil-pi", "version": version,
             "properties": [
                 {"name": "python.requires", "value": ">=3.12,<3.15"},
                 {"name": "python.runtime_dependencies", "value": "none"},
             ]},
            {"type": "application", "name": "SIGIL", "version": pin["ref"],
             "properties": [
                 {"name": "git.crates_tree", "value": pin["crates"]},
                 {"name": "git.stdlib_tree", "value": pin["stdlib"]},
             ]},
        ],
    }


def _write_reproducible_tar(source_dir, archive):
    with Path(archive).open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w") as tar:
                for path in sorted(Path(source_dir).rglob("*")):
                    relative = path.relative_to(source_dir)
                    info = tar.gettarinfo(str(path), arcname=str(relative))
                    info.uid = info.gid = 0
                    info.uname = info.gname = "root"
                    info.mtime = 0
                    if path.is_dir():
                        info.mode = 0o755
                        tar.addfile(info)
                    else:
                        info.mode = 0o755 if path.stat().st_mode & stat.S_IXUSR else 0o644
                        with path.open("rb") as stream:
                            tar.addfile(info, stream)


def build_release(*, sigil_root, output_dir, app_root=PROJECT_ROOT,
                  pin_file=None, build_runtime=True, platform_tag=None):
    app_root = Path(app_root)
    sigil_root = Path(sigil_root)
    output_dir = Path(output_dir)
    pin = _pin(pin_file or app_root / "SIGIL_REV")
    verify_sigil_source(sigil_root, pin)
    if build_runtime:
        subprocess.run(
            ["cargo", "build", "--release", "-p", "sigil-mcp"],
            cwd=sigil_root, check=True)
    runtime = sigil_root / "target" / "release" / "sigil-mcp"
    if not runtime.is_file():
        raise ReleaseBuildError(f"built sigil-mcp not found: {runtime}")
    version = (app_root / "VERSION").read_text().strip()
    if not version:
        raise ReleaseBuildError("VERSION is empty")
    platform_tag = platform_tag or f"{platform.system().lower()}-{platform.machine().lower()}"
    name = f"sigil-pi-{version}-{platform_tag}"
    output_dir.mkdir(parents=True, exist_ok=True)
    archive = output_dir / f"{name}.tar.gz"

    with tempfile.TemporaryDirectory(prefix="sigil-pi-release-") as temp:
        root = Path(temp) / name
        for relative in APP_FILES:
            _copy_file(app_root / relative, root / "app" / relative)
        for relative in DOC_FILES:
            _copy_file(app_root / relative, root / relative)
        _copy_file(app_root / "config" / "auth.example.json",
                   root / "config" / "auth.example.json")
        _copy_file(app_root / "config" / "alert-policy.json",
                   root / "config" / "alert-policy.json")
        _copy_file(app_root / "config" / "grafana-slo-dashboard.json",
                   root / "config" / "grafana-slo-dashboard.json")
        for source in sorted((app_root / "tools").iterdir()):
            if source.is_file() and source.suffix in (".sigil", ".json"):
                _copy_file(source, root / "app" / "tools" / source.name)
        _copy_file(runtime, root / "runtime" / "target" / "release" / "sigil-mcp",
                   executable=True)
        for module in STDLIB_MODULES:
            source = sigil_root / "stdlib" / "sigil" / f"{module}.sigil"
            _copy_file(source, root / "runtime" / "stdlib" / "sigil" / source.name)
        launcher = root / "bin" / "sigil-pi"
        launcher.parent.mkdir(parents=True, exist_ok=True)
        launcher.write_text(
            "#!/bin/sh\n"
            "set -eu\n"
            "release_root=$(CDPATH= cd -- \"$(dirname -- \"$0\")/..\" && pwd)\n"
            "export SIGIL_ROOT=\"$release_root/runtime\"\n"
            "exec python3 \"$release_root/app/product_main.py\" \"$@\"\n")
        launcher.chmod(0o755)
        drill_launcher = root / "bin" / "sigil-pi-release-drill"
        drill_launcher.write_text(
            "#!/bin/sh\n"
            "set -eu\n"
            "release_root=$(CDPATH= cd -- \"$(dirname -- \"$0\")/..\" && pwd)\n"
            "exec python3 \"$release_root/app/scripts/release_drill.py\" \"$@\"\n")
        drill_launcher.chmod(0o755)

        sbom_bytes = (json.dumps(_sbom(version, pin), sort_keys=True,
                                 separators=(",", ":")) + "\n").encode()
        sbom = root / "SBOM.cdx.json"
        sbom.write_bytes(sbom_bytes)
        manifest_lines = []
        for path in sorted(root.rglob("*")):
            if path.is_file() and path.name != "MANIFEST.sha256":
                manifest_lines.append(f"{_sha256(path)}  {path.relative_to(root)}")
        (root / "MANIFEST.sha256").write_text("\n".join(manifest_lines) + "\n")
        _write_reproducible_tar(Path(temp), archive)

    sbom_path = output_dir / f"{name}.SBOM.cdx.json"
    sbom_path.write_bytes(sbom_bytes)
    checksum = _sha256(archive)
    checksum_path = archive.with_suffix(archive.suffix + ".sha256")
    checksum_path.write_text(f"{checksum}  {archive.name}\n")
    return archive, checksum_path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sigil-root", required=True)
    parser.add_argument("--output", default=str(PROJECT_ROOT / "dist"))
    parser.add_argument("--no-build", action="store_true",
                        help="testing only: package an already-built runtime")
    args = parser.parse_args()
    archive, checksum = build_release(
        sigil_root=args.sigil_root, output_dir=args.output,
        build_runtime=not args.no_build)
    print(archive)
    print(checksum)


if __name__ == "__main__":
    main()
