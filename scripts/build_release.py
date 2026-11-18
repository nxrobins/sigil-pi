#!/usr/bin/env python3
"""Build a deterministic sigil-pi product release bundle."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import platform
import re
import runpy
import shutil
import stat
import subprocess
import tarfile
import tempfile
import tomllib
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
# The compiler is built from more of SIGIL than the two trees SIGIL_REV pins:
# cargo resolves the workspace's Cargo.lock and Cargo.toml, rustup and elan take
# the Rust and Lean versions from their pin files, and sigil-formal-bridge's
# build script compiles the Lean kernel under proofs/lean. Each must be the
# pinned ref's, or the bundle and its SBOM describe a build nobody pinned
# (audit 2026-11-10, F-16).
PINNED_BUILD_INPUTS = ("Cargo.lock", "Cargo.toml", "rust-toolchain.toml", "proofs/lean")
LEAN_TOOLCHAIN = "proofs/lean/lean-toolchain"
RUST_TOOLCHAIN = "rust-toolchain.toml"
CRATES_IO = "registry+https://github.com/rust-lang/crates.io-index"
# Where z3-sys finds z3.h when Z3_SYS_Z3_HEADER is unset: bindgen asks the C
# compiler, which searches these in this order.
Z3_DEFAULT_HEADERS = (Path("/usr/local/include/z3.h"), Path("/usr/include/z3.h"))

# The compiler in the bundle, and the pin of it that startup checks. The pin is
# app/SIGIL_REV because that is the file toolchain.read_rev reads beside the
# bundled toolchain.py: product_main hands toolchain.verify_binary to the
# compiler pool, which runs it before every compiler start and replacement.
RUNTIME_BINARY = "runtime/target/release/sigil-mcp"
BINARY_PIN = "app/SIGIL_REV"

# `_` is load-bearing: every Linux artifact is named `...-linux-x86_64.tar.gz`.
# The original class omitted it and the fixtures ("test-platform") never
# noticed; the real published asset was refused on first contact.
ARCHIVE_NAME_RE = re.compile(r"^sigil-pi-[A-Za-z0-9._+-]+\.tar\.gz$")


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


def verify_sigil_source(sigil_root, pin, *, compiling=True):
    """Check SIGIL against the pin; return the PINNED_BUILD_INPUTS it holds."""
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
    return _verify_build_inputs(sigil_root, pin, compiling=compiling)


def _verify_build_inputs(sigil_root, pin, *, compiling):
    """PINNED_BUILD_INPUTS are committed, clean, and the pinned ref's.

    Compared object by object, not commit by commit, for the reason SIGIL_REV
    pins trees: a SIGIL commit that touches none of them must not trip this.
    An input the pinned ref does not have at all stops a build that compiles,
    which needs it. --no-build packages a compiler built elsewhere (tests
    only), from checkouts that may hold nothing but crates/ and stdlib/; it
    goes on, and the SBOM says what it did not see. Returns the inputs held."""
    dirty = _git(sigil_root, "status", "--porcelain", "--", *PINNED_BUILD_INPUTS)
    if dirty:
        raise ReleaseBuildError(
            f"SIGIL build inputs contain uncommitted changes:\n{dirty}")
    ref = pin["ref"]
    if not _object(sigil_root, f"{ref}^{{commit}}"):
        raise ReleaseBuildError(
            f"the pinned ref {ref} is not in the SIGIL checkout, so the build inputs "
            f"outside crates/ and stdlib/ cannot be checked against it: fetch it")
    held = set()
    for path in PINNED_BUILD_INPUTS:
        pinned = _object(sigil_root, f"{ref}:{path}")
        actual = _object(sigil_root, f"HEAD:{path}")
        if actual != pinned:
            raise ReleaseBuildError(
                f"SIGIL {path} is not the pinned ref's ({ref}): "
                f"expected {pinned or 'nothing'}, got {actual or 'nothing'}")
        if pinned:
            held.add(path)
        elif compiling:
            raise ReleaseBuildError(
                f"SIGIL has no {path} at the pinned ref {ref}: the compiler is built "
                f"from it, and the SBOM lists what it holds")
    return held


def _object(sigil_root, name):
    """The git object a name resolves to in SIGIL, or None."""
    result = subprocess.run(
        ["git", "-C", str(sigil_root), "rev-parse", "--verify", "--quiet", name],
        capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else None


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


def _bundled_toolchain(app_dir):
    """The toolchain.py this bundle ships, run by path: what its entry points
    will compute at startup, computed from the same file.

    Two answers come from it. PYTHON_REQUIRES, the SBOM's python.requires: the
    one definition of the supported interpreters, which the bundle's entry
    points enforce. This was a literal here; it agreed with the support matrix
    while the entry points enforced only its floor, so the SBOM promised a range
    nothing enforced (audit F-24). And platform_key(), the suffix of the
    `sha256_<platform>` pin that verify_binary looks up on this host (audit
    F-16). The module is run from the bundle's own app/ directory, not imported
    from this checkout, so both describe the file the bundle carries. runpy
    compiles it without writing a __pycache__ into the directory being
    archived, which an import would do.
    """
    return runpy.run_path(str(Path(app_dir) / "toolchain.py"))


def _binary_pin(platform_key, digest):
    return (
        "# The compiler this bundle carries, pinned by its digest. Written by\n"
        "# scripts/build_release.py and read by toolchain.verify_binary, which\n"
        "# product_main runs before every compiler start and replacement: a\n"
        f"# {RUNTIME_BINARY} that differs from this pin is refused.\n"
        "#\n"
        "# The source the compiler was built from is in SBOM.cdx.json (component\n"
        "# SIGIL). It is not repeated here: a `ref` key would send toolchain.resolve()\n"
        "# to a cached toolchain under ~/.cache/sigil-pi/<ref> before this bundle's own.\n"
        "\n"
        f"sha256_{platform_key} = {digest}\n")


def _not_observed(what):
    return (f"not observed: this SIGIL checkout has no {what}, which only --no-build "
            f"(tests: it packages a compiler built elsewhere) allows")


def _rev_values(text):
    """`key = value` lines as toolchain.read_rev reads them."""
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values


def _z3(build_runtime):
    """(version or None, where the version came from) for the Z3 the compiler
    is compiled against: the z3_version.h beside the z3.h that z3-sys binds to.

    A build names it or stops before compiling. --no-build packages a compiler
    built elsewhere: the header Z3_SYS_Z3_HEADER names is that build's when the
    gate packages what ci.sh just built, and otherwise the SBOM says the version
    was not observed rather than naming whatever Z3 this host has.
    """
    named = os.environ.get("Z3_SYS_Z3_HEADER")
    if named:
        header = Path(named)
        if not header.is_file():
            raise ReleaseBuildError(f"Z3_SYS_Z3_HEADER names {header}, which is not a file")
    elif build_runtime:
        header = next((path for path in Z3_DEFAULT_HEADERS if path.is_file()), None)
        if header is None:
            raise ReleaseBuildError(
                "no Z3 headers: the compiler links libz3, and the SBOM names the Z3 it "
                "is built against. Set Z3_SYS_Z3_HEADER to the z3.h the build uses, as "
                "ci.sh and the release workflow do.")
    else:
        return None, ("not observed: --no-build (tests: it packages a compiler built "
                      "elsewhere) and Z3_SYS_Z3_HEADER names no header it was built against")
    version_file = header.parent / "z3_version.h"
    try:
        text = version_file.read_text()
    except OSError as error:
        raise ReleaseBuildError(
            f"no Z3 version beside {header}: {version_file} is unreadable ({error})") from error
    numbers = dict(re.findall(
        r"^#define\s+Z3_(MAJOR_VERSION|MINOR_VERSION|BUILD_NUMBER|REVISION_NUMBER)\s+(\d+)\s*$",
        text, re.M))
    if not {"MAJOR_VERSION", "MINOR_VERSION", "BUILD_NUMBER"} <= set(numbers):
        raise ReleaseBuildError(f"{version_file} does not state the Z3 version")
    parts = [numbers["MAJOR_VERSION"], numbers["MINOR_VERSION"], numbers["BUILD_NUMBER"]]
    if numbers.get("REVISION_NUMBER", "0") != "0":
        parts.append(numbers["REVISION_NUMBER"])
    return ".".join(parts), str(version_file)


def _toml(sigil_root, relative):
    path = Path(sigil_root) / relative
    try:
        return tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ReleaseBuildError(f"SIGIL {relative} is unreadable: {error}") from error


def _lean_toolchain(sigil_root):
    """(the toolchain as written, its name, its version) from SIGIL's pin of it."""
    try:
        written = (Path(sigil_root) / LEAN_TOOLCHAIN).read_text().strip()
    except OSError as error:
        raise ReleaseBuildError(f"SIGIL {LEAN_TOOLCHAIN} is unreadable: {error}") from error
    repository, _, version = written.rpartition(":")
    if not repository or not version:
        raise ReleaseBuildError(f"SIGIL {LEAN_TOOLCHAIN} names no toolchain: {written!r}")
    return written, repository.rsplit("/", 1)[-1], version


def _rust_channel(sigil_root):
    channel = _toml(sigil_root, RUST_TOOLCHAIN).get("toolchain", {}).get("channel")
    if not isinstance(channel, str) or not channel:
        raise ReleaseBuildError(f"SIGIL {RUST_TOOLCHAIN} names no channel")
    return channel


def _dependency_names(table, inherited):
    """The package names a manifest's dependency table refers to."""
    names = set()
    for key, spec in (table or {}).items():
        if isinstance(spec, dict) and spec.get("workspace"):
            spec = inherited.get(key, {})
        names.add(spec.get("package", key) if isinstance(spec, dict) else key)
    return names


def _dev_only(manifest, inherited):
    """Names a workspace member depends on only to test itself."""
    kinds = {"normal": set(), "dev": set()}
    for table in (manifest, *(manifest.get("target") or {}).values()):
        for section, kind in (("dependencies", "normal"), ("build-dependencies", "normal"),
                              ("build_dependencies", "normal"), ("dev-dependencies", "dev"),
                              ("dev_dependencies", "dev")):
            kinds[kind] |= _dependency_names(table.get(section), inherited)
    return kinds["dev"] - kinds["normal"]


def _workspace_members(sigil_root, workspace, root_manifest):
    """{package name: manifest} for each member of SIGIL's workspace."""
    root = Path(sigil_root)
    members = {}
    if "package" in root_manifest:
        members[root_manifest["package"]["name"]] = root_manifest
    for pattern in workspace.get("members", []):
        directories = (sorted(root.glob(pattern)) if any(c in pattern for c in "*?[")
                       else [root / pattern])
        for directory in directories:
            manifest = _toml(root, (directory / "Cargo.toml").relative_to(root))
            members[manifest["package"]["name"]] = manifest
    return members


def _compiler_crates(sigil_root):
    """Every package SIGIL's Cargo.lock resolves for sigil-mcp.

    The closure of sigil-mcp over the lock's dependency edges, less the edges a
    workspace member declares only as dev-dependencies (the lock records those
    for members, and they build tests, not the compiler). The lock resolves
    every feature and every target, so this is a superset of what one
    platform's build links; the SBOM says so.
    """
    lock = _toml(sigil_root, "Cargo.lock")
    # Without the workspace manifest (--no-build only) no edge is known to be
    # dev-only, and the list stays a superset.
    root_manifest = (_toml(sigil_root, "Cargo.toml")
                     if (Path(sigil_root) / "Cargo.toml").is_file() else {})
    workspace = root_manifest.get("workspace", {})
    inherited = workspace.get("dependencies", {})
    members = _workspace_members(sigil_root, workspace, root_manifest)
    by_name = {}
    for package in lock.get("package", []):
        by_name.setdefault(package["name"], []).append(package)

    def resolve(entry):
        # Cargo.lock writes "name", "name version" or "name version (source)",
        # whichever is the shortest that is unique in the lock.
        name, _, rest = entry.partition(" ")
        version, _, source = rest.partition(" ")
        found = [package for package in by_name.get(name, [])
                 if version in ("", package["version"])
                 and source in ("", f"({package.get('source')})")]
        if len(found) != 1:
            raise ReleaseBuildError(
                f"SIGIL Cargo.lock resolves {entry!r} to {len(found)} packages")
        return found[0]

    if len(by_name.get("sigil-mcp", [])) != 1:
        raise ReleaseBuildError("SIGIL Cargo.lock does not hold exactly one sigil-mcp")
    closure = {}
    pending = list(by_name["sigil-mcp"])
    while pending:
        package = pending.pop()
        key = (package["name"], package["version"], package.get("source", ""))
        if key in closure:
            continue
        closure[key] = package
        entries = package.get("dependencies", [])
        if "source" not in package and package["name"] in members:
            dev_only = _dev_only(members[package["name"]], inherited)
            entries = [entry for entry in entries if entry.partition(" ")[0] not in dev_only]
        pending.extend(resolve(entry) for entry in entries)
    return [closure[key] for key in sorted(closure)]


def _crate_component(package):
    name, version = package["name"], package["version"]
    source = package.get("source")
    component = {"type": "library", "name": name, "version": version,
                 "properties": [{"name": "cargo.source", "value": source or "path"}]}
    if source == CRATES_IO:
        component["purl"] = component["bom-ref"] = f"pkg:cargo/{name}@{version}"
    else:
        component["bom-ref"] = f"cargo:{name}@{version} ({source or 'path'})"
    if package.get("checksum"):
        component["hashes"] = [{"alg": "SHA-256", "content": package["checksum"]}]
    return component


def _sbom(version, pin, python_requires, *, crates, z3, lean, rust_channel):
    """What the bundle is, and what its compiler is made of (audit F-16).

    The compiler links SIGIL's crates and the Rust standard library, and SIGIL's
    Lean kernel with the Lean runtime, statically; it loads libz3 from the host.
    Each toolchain entry names the file its version was read from.
    """
    ref = pin["ref"]
    serial_seed = f"sigil-pi:{version}:{ref}"
    z3_version, z3_source = z3
    toolchain_entries = []
    if lean:
        lean_written, lean_name, lean_version = lean
        lean_entry = {"name": lean_name, "version": lean_version, "properties": [
            {"name": "toolchain", "value": lean_written},
            {"name": "version.source", "value": f"SIGIL {ref}:{LEAN_TOOLCHAIN}"}]}
    else:
        lean_entry = {"name": "lean4", "properties": [
            {"name": "version.source", "value": _not_observed(LEAN_TOOLCHAIN)}]}
    if rust_channel:
        rust_entry = {"version": rust_channel, "properties": [
            {"name": "version.source", "value": f"SIGIL {ref}:{RUST_TOOLCHAIN}"}]}
    else:
        rust_entry = {"properties": [
            {"name": "version.source", "value": _not_observed(RUST_TOOLCHAIN)}]}
    toolchain_entries.append({
        "type": "library", "bom-ref": "lean-toolchain", **lean_entry,
        "properties": [
            {"name": "linkage", "value": (
                "static: SIGIL's Lean kernel is compiled to C with this toolchain "
                "and linked into sigil-mcp with its runtime archives (libInit, "
                "libleanrt and the libraries shipped beside them)")},
            *lean_entry["properties"]]})
    toolchain_entries.append({
        "type": "library", "bom-ref": "rust-toolchain", "name": "rust", **rust_entry,
        "properties": [
            {"name": "linkage", "value": (
                "static: the standard library of this Rust toolchain is linked "
                "into sigil-mcp")},
            *rust_entry["properties"]]})
    if crates is None:
        cargo = [{"name": "cargo.lock", "value": f"none at {ref}"},
                 {"name": "cargo.scope", "value": _not_observed("Cargo.lock")}]
    else:
        cargo = [{"name": "cargo.lock", "value": f"{ref}:Cargo.lock"},
                 {"name": "cargo.scope", "value": (
                     "the crates listed are every package Cargo.lock resolves for "
                     "sigil-mcp's normal and build dependencies, on every target and "
                     "with every feature: a superset of what one platform's build links")}]
    solver = {"type": "library", "bom-ref": "z3-library", "name": "z3",
              "properties": [
                  {"name": "linkage", "value": "dynamic: sigil-mcp loads libz3 from "
                                               "the host at run time; the bundle does "
                                               "not carry it"},
                  {"name": "version.source", "value": z3_source},
              ]}
    if z3_version:
        solver["version"] = z3_version
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "serialNumber": f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, serial_seed)}",
        "version": 1,
        "metadata": {"component": {
            "type": "application", "name": "sigil-pi", "version": version,
        }},
        "components": [
            {"type": "application", "bom-ref": "sigil-pi", "name": "sigil-pi",
             "version": version,
             "properties": [
                 {"name": "python.requires", "value": python_requires},
                 {"name": "python.runtime_dependencies", "value": "none"},
             ]},
            {"type": "application", "bom-ref": "SIGIL", "name": "SIGIL", "version": ref,
             "properties": [
                 {"name": "git.crates_tree", "value": pin["crates"]},
                 {"name": "git.stdlib_tree", "value": pin["stdlib"]},
                 *cargo,
             ]},
            solver,
            *toolchain_entries,
            *(_crate_component(package) for package in crates or ()),
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
    held = verify_sigil_source(sigil_root, pin, compiling=build_runtime)
    # Everything the SBOM states is read before the build, so a checkout that
    # cannot be described stops before a compile it would waste.
    z3 = _z3(build_runtime)
    crates = _compiler_crates(sigil_root) if "Cargo.lock" in held else None
    lean = _lean_toolchain(sigil_root) if "proofs/lean" in held else None
    rust_channel = _rust_channel(sigil_root) if RUST_TOOLCHAIN in held else None
    if build_runtime:
        # Solver-verifying, exactly like ci.sh step 1 (the comment there says
        # why): the product client never sets SIGIL_ALLOW_UNVERIFIED_CERT, so
        # a solver-off compiler would ship a product that fails closed on its
        # first forge. z3-sys takes Z3_SYS_Z3_HEADER and the linker search
        # path from the environment, which ci.sh provides. --locked: cargo
        # builds from the pinned Cargo.lock the SBOM lists, or stops; it never
        # resolves a different one.
        subprocess.run(
            ["cargo", "build", "--release", "--locked", "-p", "sigil-mcp",
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
        binary = root / RUNTIME_BINARY
        _copy_file(runtime, binary, executable=True)
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

        bundled = _bundled_toolchain(root / "app")
        (root / BINARY_PIN).write_text(
            _binary_pin(bundled["platform_key"](), _sha256(binary)))
        document = _sbom(version, pin, bundled["PYTHON_REQUIRES"], crates=crates,
                         z3=z3, lean=lean, rust_channel=rust_channel)
        sbom_bytes = (json.dumps(document, sort_keys=True,
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

        # The compiler pin (audit F-16). A candidate that carries one must pin
        # the compiler it carries, or it cannot start. Candidates built before
        # bundles carried a pin (v0.4.0 and earlier) have none, and their
        # startup checks no digest; they verify as they did.
        if BINARY_PIN in files:
            if RUNTIME_BINARY not in files:
                raise ReleaseBuildError(
                    f"candidate pins a compiler but carries no {RUNTIME_BINARY}")
            pins = sorted(value for key, value in
                          _rev_values(read(BINARY_PIN).decode()).items()
                          if key.startswith("sha256_"))
            carried = hashlib.sha256(read(RUNTIME_BINARY)).hexdigest()
            if not pins or set(pins) != {carried}:
                raise ReleaseBuildError(
                    f"candidate's compiler pin {pins} is not the compiler it carries "
                    f"({carried}): its startup would refuse it")

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
