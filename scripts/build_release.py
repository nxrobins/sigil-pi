#!/usr/bin/env python3
"""Build a deterministic sigil-pi product release bundle."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import platform
import re
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
    "docs/runbooks.md",
    "docs/security-guarantee.md",
    "docs/security/threat-model.md",
    "docs/state-compatibility.md",
    "docs/support-matrix.md",
)
STDLIB_MODULES = ("http", "json", "kv")

# Documents an operator ACTS on, pinned to the repo copy at verification time.
#
# Freezing the candidate creates a hazard it is worth naming: the archive an
# operator holds could quietly diverge from the repo everyone else reads. For
# instructions — the API contract, the operations guide, the runbooks, the
# threat model, the state-compatibility rules — that divergence is the 3am
# failure, so a mismatch fails the gate and demands a fresh candidate.
#
# Documents that RECORD status are deliberately absent, and must stay absent:
# README.md, CHANGELOG.md, SECURITY.md, docs/capacity.md, docs/support-matrix.md.
# Parity on those would reinstate exactly the circularity the freeze removes,
# because the readiness process is REQUIRED to edit them as evidence lands.
# tests/test_release_build.py::test_payload_parity_never_covers_a_recording_document
# pins that split.
PAYLOAD_PARITY_FILES = (
    "docs/api.md",
    "docs/operations.md",
    "docs/runbooks.md",
    "docs/security/threat-model.md",
    "docs/state-compatibility.md",
)

CANDIDATE_SCHEMA = 1
CANDIDATE_FIELDS = (
    "version", "tag", "platform_tag", "file", "sha256",
    "sigil_ref", "crates_tree", "stdlib_tree", "release_asset", "rollback_from",
)
ARCHIVE_NAME_RE = re.compile(r"^sigil-pi-[A-Za-z0-9.+-]+\.tar\.gz$")


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
        # Solver-verifying, exactly like ci.sh step 1 (the comment there says
        # why): the product client never sets SIGIL_ALLOW_UNVERIFIED_CERT, so
        # a solver-off compiler would ship a product that fails closed on its
        # first forge. z3-sys takes Z3_SYS_Z3_HEADER and the linker search
        # path from the environment, which ci.sh provides.
        subprocess.run(
            ["cargo", "build", "--release", "-p", "sigil-mcp",
             "--features", "sigil-mcp/solver"],
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


def load_candidate_record(path=None):
    """The frozen candidate's pin: what the gate verifies instead of rebuilding.

    A release is built ONCE, published, and then referred to by digest. This
    record is how every later step — evidence collection, the readiness gate,
    a rollback drill — names the same bytes. It is filled in from the PUBLISHED
    asset, never from a local build: a record generated beside a rebuild would
    always agree with it, which makes the check decorative.
    """
    path = Path(path or PROJECT_ROOT / "docs" / "evidence" / "candidate.json")
    if not path.is_file():
        raise ReleaseBuildError(
            f"no frozen candidate record at {path}. A release is built once, "
            f"published, and then verified by digest — see docs/evidence/README.md.")
    try:
        record = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ReleaseBuildError(f"candidate record is unreadable: {path}") from error
    if not isinstance(record, dict) or record.get("schema_version") != CANDIDATE_SCHEMA:
        raise ReleaseBuildError(
            f"candidate record schema must be {CANDIDATE_SCHEMA}")
    missing = [field for field in CANDIDATE_FIELDS if not record.get(field)]
    if missing:
        raise ReleaseBuildError(f"candidate record is missing: {missing}")
    if not re.fullmatch(r"[0-9a-f]{64}", str(record["sha256"])):
        raise ReleaseBuildError("candidate sha256 must be 64 lowercase hex characters")
    if not ARCHIVE_NAME_RE.fullmatch(str(record["file"])):
        raise ReleaseBuildError(f"candidate file name is unsafe: {record['file']!r}")
    if record["tag"] != f"v{record['version']}":
        raise ReleaseBuildError("candidate tag must be v<version>")
    asset = str(record["release_asset"])
    expected = f"/releases/download/{record['tag']}/{record['file']}"
    if not asset.startswith("https://") or expected not in asset:
        raise ReleaseBuildError(
            f"candidate release_asset must be an https URL ending {expected}")
    rollback = record["rollback_from"]
    if (not isinstance(rollback, dict) or not rollback.get("version")
            or not re.fullmatch(r"[0-9a-f]{64}", str(rollback.get("sha256", "")))):
        raise ReleaseBuildError(
            "candidate rollback_from must name a distinct version and its sha256")
    if rollback["sha256"] == record["sha256"] or rollback["version"] == record["version"]:
        raise ReleaseBuildError("candidate cannot roll back to itself")
    return record


def _archive_members(archive, prefix):
    """Every regular file in the archive, by its path below the single root."""
    files = {}
    for member in archive.getmembers():
        if member.isdir():
            continue
        if not member.isfile():
            raise ReleaseBuildError(f"archive holds a non-regular member: {member.name}")
        head, _, relative = member.name.partition("/")
        if head != prefix or not relative:
            raise ReleaseBuildError(
                f"archive holds more than one top-level directory: {member.name}")
        files[relative] = member
    return files


def verify_release(archive, *, record, app_root=PROJECT_ROOT):
    """Prove an archive IS the frozen candidate, without rebuilding it.

    Rebuilding was the bug: the candidate was a function of the working tree, so
    recording an evidence result moved the digest that evidence bound to
    (measured 2026-08-23). Everything below re-derives from the bytes on disk.
    """
    archive = Path(archive)
    if archive.name != record["file"]:
        raise ReleaseBuildError(
            f"candidate file name mismatch: expected {record['file']}, got {archive.name}")
    if archive.is_symlink() or not archive.is_file():
        raise ReleaseBuildError(f"candidate archive is not a regular file: {archive}")
    digest = _sha256(archive)
    if digest != record["sha256"]:
        raise ReleaseBuildError(
            f"candidate digest does not match the record:\n"
            f"  recorded {record['sha256']}\n  actual   {digest}")

    with tarfile.open(archive, "r:gz") as opened:
        names = opened.getnames()
        if not names:
            raise ReleaseBuildError("candidate archive is empty")
        prefix = names[0].split("/", 1)[0]
        files = _archive_members(opened, prefix)
        for required in ("MANIFEST.sha256", "SBOM.cdx.json", "app/VERSION"):
            if required not in files:
                raise ReleaseBuildError(f"candidate archive lacks {required}")

        def read(relative):
            return opened.extractfile(files[relative]).read()

        # The inner manifest, so recomputing the outer digest is not enough to
        # pass a tampered bundle.
        for line in read("MANIFEST.sha256").decode().splitlines():
            expected_hash, _, relative = line.partition("  ")
            if not relative:
                continue
            if relative not in files:
                raise ReleaseBuildError(f"manifest names a missing file: {relative}")
            if hashlib.sha256(read(relative)).hexdigest() != expected_hash:
                raise ReleaseBuildError(f"manifest checksum failed for {relative}")

        version = read("app/VERSION").decode().strip()
        if version != record["version"]:
            raise ReleaseBuildError(
                f"candidate VERSION is {version}, record says {record['version']}")

        # The SBOM's SIGIL pin is checked against the RECORD, never the tree's
        # current SIGIL_REV: a toolchain bump during a pilot must not fail the
        # gate for a candidate that was built correctly before it.
        sbom = json.loads(read("SBOM.cdx.json"))
        sigil = next((c for c in sbom.get("components", [])
                      if c.get("name") == "SIGIL"), None)
        properties = {p["name"]: p["value"] for p in (sigil or {}).get("properties", [])}
        if (sigil is None or sigil.get("version") != record["sigil_ref"]
                or properties.get("git.crates_tree") != record["crates_tree"]
                or properties.get("git.stdlib_tree") != record["stdlib_tree"]):
            raise ReleaseBuildError(
                "candidate SBOM does not carry the recorded SIGIL pin")

        # Parity: an operator's instructions must still be the repo's.
        for relative in PAYLOAD_PARITY_FILES:
            live = Path(app_root) / relative
            if relative not in files or not live.is_file():
                raise ReleaseBuildError(f"payload is stale: {relative} is missing")
            if hashlib.sha256(read(relative)).hexdigest() != _sha256(live):
                raise ReleaseBuildError(
                    f"payload is stale: {relative} has changed since the candidate "
                    f"was built. Cut a new candidate and re-collect evidence.")

    return {"sha256": digest, "version": version,
            "platform_tag": record["platform_tag"], "sigil_ref": record["sigil_ref"]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sigil-root")
    parser.add_argument("--output", default=str(PROJECT_ROOT / "dist"))
    parser.add_argument("--no-build", action="store_true",
                        help="testing only: package an already-built runtime")
    parser.add_argument("--verify", metavar="ARCHIVE",
                        help="verify a published candidate instead of building one")
    parser.add_argument("--record", help="path to the candidate record")
    args = parser.parse_args()
    if args.verify:
        result = verify_release(args.verify,
                                record=load_candidate_record(args.record))
        print(json.dumps(result, indent=2, sort_keys=True))
        return
    if not args.sigil_root:
        parser.error("--sigil-root is required unless --verify is given")
    archive, checksum = build_release(
        sigil_root=args.sigil_root, output_dir=args.output,
        build_runtime=not args.no_build)
    print(archive)
    print(checksum)


if __name__ == "__main__":
    main()
