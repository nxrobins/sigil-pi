"""A release bundle lists what it carries, and pins the compiler it carries.

Audit 2026-11-10, F-16. The v0.4.0 SBOM listed two components, sigil-pi and
SIGIL, while the compiler in the bundle is built from a Cargo.lock of 338
packages, links a kernel built with Lean together with Lean's runtime, and
loads the host's libz3. And at run time toolchain.verify_binary, which
product_main runs before every compiler start and replacement, had nothing to
compare: no bundle carried a `sha256_<platform>` pin, so it returned None and a
bundle ran whatever binary sat in its runtime/ directory.

The fake SIGIL checkout of tests/test_release_build.py pins the SBOM's shape
exactly. The real one (SIGIL_ROOT, as the gate sets it) checks it against
SIGIL's own Cargo.lock, and boots a real bundle to show that one changed byte
in its compiler stops startup.
"""

import hashlib
import json
import os
import re
import socket
import subprocess
import tarfile
import time
import tomllib
import urllib.request

import pytest

from conftest import PI_ROOT, needs_toolchain

import scripts.build_release as build_module
import toolchain
from scripts.build_release import (
    ReleaseBuildError,
    _pin,
    _write_reproducible_tar,
    build_release,
    load_candidate_record,
    verify_release,
)
from test_release_build import (
    CRATES_IO,
    FAKE_COMPILER_CRATES,
    FAKE_LEAN_TOOLCHAIN,
    FAKE_RUST_CHANNEL,
    _fake_sigil,
    _git,
    _record_for,
    fake_checksum,
)

RUNTIME = "runtime/target/release/sigil-mcp"
PIN_FILE = "app/SIGIL_REV"
# How long a booted bundle may take to fail or to become ready. Healthy, both
# take a few seconds; the margin is for a Mac that is slow to start a program
# it has not run before (the launcher and the compiler copy are both new).
STARTUP_WAIT_S = 180

# The header an official Z3 release ships beside z3.h (and Homebrew's has the
# same lines): the version of the Z3 the compiler was compiled against.
Z3_VERSION_H = (
    "// automatically generated file.\n"
    "#define Z3_MAJOR_VERSION   4\n"
    "#define Z3_MINOR_VERSION   12\n"
    "#define Z3_BUILD_NUMBER    2\n"
    "#define Z3_REVISION_NUMBER 0\n"
    "\n"
    '#define Z3_FULL_VERSION    "4.12.2.0"\n')


@pytest.fixture
def z3_header(tmp_path, monkeypatch):
    """A Z3 install laid out as the release workflow unzips one, named by
    Z3_SYS_Z3_HEADER as the workflow and ci.sh name theirs."""
    include = tmp_path / "z3-pinned" / "include"
    include.mkdir(parents=True)
    (include / "z3.h").write_text("/* the API z3-sys binds to */\n")
    (include / "z3_version.h").write_text(Z3_VERSION_H)
    monkeypatch.setenv("Z3_SYS_Z3_HEADER", str(include / "z3.h"))
    return include / "z3_version.h"


def _build(tmp_path, sigil, pin, out="dist"):
    archive, _ = build_release(
        sigil_root=sigil, output_dir=tmp_path / out, app_root=PI_ROOT,
        pin_file=pin, build_runtime=False, platform_tag="test-platform")
    return archive


def _sbom_of(archive):
    return json.loads(archive.with_name(
        archive.name.removesuffix(".tar.gz") + ".SBOM.cdx.json").read_text())


def _properties(component):
    return {p["name"]: p["value"] for p in component.get("properties", [])}


def _crates(sbom):
    return {(c["name"], c["version"]): c for c in sbom["components"]
            if "cargo.source" in _properties(c)}


def _component(sbom, name):
    """The one component of that name that is not a crate (the z3 crate is not Z3)."""
    found = [c for c in sbom["components"]
             if c["name"] == name and "cargo.source" not in _properties(c)]
    assert len(found) == 1, (
        f"the SBOM has {len(found)} components named {name!r}; it lists "
        f"{sorted(c['name'] for c in sbom['components'])[:12]}")
    return found[0]


def _members(archive):
    with tarfile.open(archive, "r:gz") as opened:
        return {member.name.split("/", 1)[1]: opened.extractfile(member).read()
                for member in opened.getmembers() if member.isfile()}


def _install(archive, into):
    into.mkdir(parents=True)
    with tarfile.open(archive, "r:gz") as opened:
        opened.extractall(into, filter="data")
    [root] = list(into.iterdir())
    return root


def _commit(root, message):
    _git(root, "-c", "user.name=test", "-c", "user.email=test@example.invalid",
         "commit", "-qam", message)


def _bare_sigil(tmp_path):
    """A checkout holding only crates/ and stdlib/, as the drill tests' are,
    pinned at its own ref."""
    sigil, pin = _fake_sigil(tmp_path)
    _git(sigil, "rm", "-rq", "Cargo.lock", "Cargo.toml", "rust-toolchain.toml", "proofs")
    _commit(sigil, "only crates and stdlib")
    pin.write_text(pin.read_text().replace(_pin(pin)["ref"], _git(sigil, "rev-parse", "HEAD")))
    return sigil, pin


def _without_cargo(monkeypatch):
    """subprocess.run, except that running cargo fails the test."""
    compiled = []
    run = subprocess.run

    def no_cargo(command, *args, **kwargs):
        if command[0] == "cargo":
            compiled.append(command)
            raise AssertionError(f"the build ran {command}")
        return run(command, *args, **kwargs)

    monkeypatch.setattr(build_module.subprocess, "run", no_cargo)
    return compiled


# ── the SBOM ───────────────────────────────────────────────────────────────


def test_the_sbom_lists_the_crates_the_lock_resolves_for_the_compiler(tmp_path, z3_header):
    sigil, pin = _fake_sigil(tmp_path)
    sbom = _sbom_of(_build(tmp_path, sigil, pin))
    crates = _crates(sbom)
    assert set(crates) == FAKE_COMPILER_CRATES, (
        f"the SBOM lists {sorted(set(crates) - FAKE_COMPILER_CRATES)} that sigil-mcp is not "
        f"built from, and misses {sorted(FAKE_COMPILER_CRATES - set(crates))} that it is")
    for (name, version), crate in crates.items():
        assert crate["type"] == "library"
        if name.startswith("sigil-"):
            # SIGIL's own crates, at the ref the SIGIL component names.
            assert _properties(crate)["cargo.source"] == "path"
            assert "purl" not in crate and "hashes" not in crate
        else:
            assert _properties(crate)["cargo.source"] == CRATES_IO
            assert crate["purl"] == f"pkg:cargo/{name}@{version}"
            assert crate["hashes"] == [
                {"alg": "SHA-256", "content": fake_checksum(name, version)}]
    references = [component["bom-ref"] for component in sbom["components"]]
    assert len(references) == len(set(references)), "bom-refs must be unique"
    sigil_properties = _properties(_component(sbom, "SIGIL"))
    assert sigil_properties["cargo.lock"] == f"{_pin(pin)['ref']}:Cargo.lock"
    assert "superset" in sigil_properties["cargo.scope"]


def test_the_sbom_names_the_solver_library_and_where_its_version_comes_from(
        tmp_path, z3_header):
    sigil, pin = _fake_sigil(tmp_path)
    z3 = _component(_sbom_of(_build(tmp_path, sigil, pin)), "z3")
    assert z3["version"] == "4.12.2"
    assert _properties(z3)["version.source"] == str(z3_header)
    # Not in the bundle: the compiler loads it from the host.
    assert _properties(z3)["linkage"].startswith("dynamic")


def test_the_sbom_names_the_lean_and_rust_toolchains_and_where_their_versions_come_from(
        tmp_path, z3_header):
    sigil, pin = _fake_sigil(tmp_path)
    ref = _pin(pin)["ref"]
    sbom = _sbom_of(_build(tmp_path, sigil, pin))
    lean = _component(sbom, "lean4")
    assert lean["version"] == "v4.32.0-rc1"
    assert _properties(lean)["toolchain"] == FAKE_LEAN_TOOLCHAIN
    assert _properties(lean)["version.source"] == f"SIGIL {ref}:proofs/lean/lean-toolchain"
    assert _properties(lean)["linkage"].startswith("static")
    rust = _component(sbom, "rust")
    assert rust["version"] == FAKE_RUST_CHANNEL
    assert _properties(rust)["version.source"] == f"SIGIL {ref}:rust-toolchain.toml"
    assert _properties(rust)["linkage"].startswith("static")


def test_a_packaged_compiler_whose_z3_was_not_seen_says_so(tmp_path, monkeypatch):
    """--no-build (tests only) packages a compiler built elsewhere. Without the
    header that build used, the SBOM states that it does not know the version,
    rather than naming whatever Z3 this host happens to have."""
    monkeypatch.delenv("Z3_SYS_Z3_HEADER", raising=False)
    sigil, pin = _fake_sigil(tmp_path)
    z3 = _component(_sbom_of(_build(tmp_path, sigil, pin)), "z3")
    assert "version" not in z3
    assert "not observed" in _properties(z3)["version.source"]


def test_a_build_with_no_z3_headers_stops_before_it_compiles(tmp_path, monkeypatch):
    """A build that compiles the compiler must name the Z3 it compiles against."""
    monkeypatch.delenv("Z3_SYS_Z3_HEADER", raising=False)
    monkeypatch.setattr(build_module, "Z3_DEFAULT_HEADERS",
                        (tmp_path / "nowhere" / "z3.h",), raising=False)
    sigil, pin = _fake_sigil(tmp_path)
    compiled = _without_cargo(monkeypatch)
    with pytest.raises(ReleaseBuildError, match="Z3"):
        build_release(sigil_root=sigil, output_dir=tmp_path / "out", app_root=PI_ROOT,
                      pin_file=pin, build_runtime=True, platform_tag="test")
    assert compiled == []


@pytest.mark.parametrize("path", [
    "Cargo.lock",
    "Cargo.toml",
    "rust-toolchain.toml",
    "proofs/lean/lean-toolchain",
    "proofs/lean/LambdaSigil/CombinedKernel.lean",
])
def test_the_build_takes_its_inputs_outside_crates_from_the_pinned_ref(
        tmp_path, z3_header, path):
    """SIGIL_REV pins the crates/ and stdlib/ trees, but the compiler is also
    built from SIGIL's Cargo.lock and Cargo.toml, its Rust and Lean toolchain
    pins, and the Lean kernel under proofs/lean. Each must be the pinned ref's,
    or the bundle and its SBOM describe a build nobody pinned."""
    sigil, pin = _fake_sigil(tmp_path)
    target = sigil / path
    target.write_text(target.read_text() + "\n# moved\n")
    with pytest.raises(ReleaseBuildError, match="uncommitted"):
        _build(tmp_path, sigil, pin, out="dirty")
    _commit(sigil, f"move {path}")
    # crates/ and stdlib/ did not move, so the tree pin alone still matches.
    assert _git(sigil, "rev-parse", "HEAD:crates") == _pin(pin)["crates"]
    with pytest.raises(ReleaseBuildError, match="pinned ref"):
        _build(tmp_path, sigil, pin, out="committed")


def test_the_compiler_is_built_from_the_lock_the_sbom_lists(tmp_path, z3_header, monkeypatch):
    """--locked: cargo builds from the pinned Cargo.lock or stops. Without it,
    cargo may resolve again and build crates the SBOM does not list."""
    sigil, pin = _fake_sigil(tmp_path)
    compiled = _without_cargo(monkeypatch)
    with pytest.raises(AssertionError, match="the build ran"):
        build_release(sigil_root=sigil, output_dir=tmp_path / "out", app_root=PI_ROOT,
                      pin_file=pin, build_runtime=True, platform_tag="test")
    assert compiled == [["cargo", "build", "--release", "--locked", "-p", "sigil-mcp",
                         "--features", "sigil-mcp/solver"]]


def test_a_build_that_compiles_needs_every_input_it_compiles_from(
        tmp_path, z3_header, monkeypatch):
    sigil, pin = _bare_sigil(tmp_path)
    compiled = _without_cargo(monkeypatch)
    with pytest.raises(ReleaseBuildError, match="has no Cargo.lock at the pinned ref"):
        build_release(sigil_root=sigil, output_dir=tmp_path / "out", app_root=PI_ROOT,
                      pin_file=pin, build_runtime=True, platform_tag="test")
    assert compiled == []


def test_a_package_of_a_bare_checkout_says_what_it_did_not_see(tmp_path, z3_header):
    """--no-build (tests only) packages a compiler built elsewhere, and the
    drill tests do it from checkouts that hold only crates/ and stdlib/. The
    SBOM then lists no crates and no toolchain versions, and says why."""
    sigil, pin = _bare_sigil(tmp_path)
    sbom = _sbom_of(_build(tmp_path, sigil, pin))
    assert _crates(sbom) == {}
    assert "not observed" in _properties(_component(sbom, "SIGIL"))["cargo.scope"]
    for name in ("lean4", "rust"):
        component = _component(sbom, name)
        assert "version" not in component
        assert "not observed" in _properties(component)["version.source"]


# ── the pin ────────────────────────────────────────────────────────────────


def test_the_bundle_pins_the_compiler_it_carries(tmp_path, z3_header):
    sigil, pin = _fake_sigil(tmp_path)
    members = _members(_build(tmp_path, sigil, pin))
    assert PIN_FILE in members, (
        "the bundle carries no app/SIGIL_REV, so toolchain.verify_binary finds no "
        "sha256_<platform> pin and every compiler start and replacement checks nothing")
    bundled = tmp_path / "bundled-SIGIL_REV"
    bundled.write_bytes(members[PIN_FILE])
    rev = toolchain.read_rev(bundled)
    # Exactly the one pin. No `ref`: with one, toolchain.resolve() would look for
    # a cached toolchain under ~/.cache/sigil-pi/<ref> before the bundle's own.
    assert rev == {f"sha256_{toolchain.platform_key()}":
                   hashlib.sha256(members[RUNTIME]).hexdigest()}, rev
    manifest = members["MANIFEST.sha256"].decode().splitlines()
    assert f"{hashlib.sha256(members[PIN_FILE]).hexdigest()}  {PIN_FILE}" in manifest
    # The function product_main hands every compiler start and replacement.
    binary = tmp_path / "sigil-mcp"
    binary.write_bytes(members[RUNTIME])
    assert toolchain.verify_binary(binary, rev) is None
    changed = bytearray(members[RUNTIME])
    changed[len(changed) // 2] ^= 0x01
    binary.write_bytes(bytes(changed))
    assert "does not match the SIGIL_REV pin" in (toolchain.verify_binary(binary, rev) or "")


def _repack(archive, tmp_path, change):
    """The archive with ``change(root)`` applied and its MANIFEST recomputed the
    way the build computes it, so only the pin check can tell."""
    root = _install(archive, tmp_path / "repack" / "tree")
    change(root)
    lines = [f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.relative_to(root)}"
             for path in sorted(root.rglob("*"))
             if path.is_file() and path.name != "MANIFEST.sha256"]
    (root / "MANIFEST.sha256").write_text("\n".join(lines) + "\n")
    repacked = tmp_path / "repack" / "dist" / archive.name
    repacked.parent.mkdir(parents=True)
    _write_reproducible_tar(root.parent, repacked)
    return repacked


def test_verify_refuses_a_candidate_whose_pin_is_not_its_compiler(tmp_path, z3_header):
    """A candidate that pins another binary cannot start; the gate says so first."""
    sigil, pin = _fake_sigil(tmp_path)
    key = f"sha256_{toolchain.platform_key()}"
    wrong = _repack(_build(tmp_path, sigil, pin), tmp_path,
                    lambda root: (root / PIN_FILE).write_text(f"{key} = {'0' * 64}\n"))
    record = load_candidate_record(_record_for(wrong, tmp_path / "candidate.json", pin))
    with pytest.raises(ReleaseBuildError, match="pin"):
        verify_release(wrong, record=record, app_root=PI_ROOT)


def test_verify_still_accepts_a_candidate_from_before_the_pin(tmp_path, z3_header):
    """v0.4.0, the candidate product-ci.sh verifies, was built before bundles
    carried a pin; its bytes are what they were, and they still verify."""
    sigil, pin = _fake_sigil(tmp_path)
    legacy = _repack(_build(tmp_path, sigil, pin), tmp_path,
                     lambda root: (root / PIN_FILE).unlink())
    record = load_candidate_record(_record_for(legacy, tmp_path / "candidate.json", pin))
    assert verify_release(legacy, record=record, app_root=PI_ROOT)["sha256"] == record["sha256"]


def test_the_release_gate_installs_the_lock_with_hash_checking():
    """As ci.sh and ci.yml do. The lock requires hashes by itself too."""
    text = (PI_ROOT / ".github" / "workflows" / "release.yml").read_text()
    installs = re.findall(r"pip install[^\n]*", text)
    assert installs, "release.yml installs nothing; this guard reads the wrong file"
    assert all("--require-hashes" in line for line in installs), installs


# ── a real bundle, against SIGIL_ROOT as the gate sets it ──────────────────


@pytest.fixture(scope="module")
def real_bundle(tmp_path_factory):
    needs_toolchain()
    archive, _ = build_release(
        sigil_root=toolchain.resolve().stdlib_repo,
        output_dir=tmp_path_factory.mktemp("pinned-bundle"),
        app_root=PI_ROOT, build_runtime=False)
    return archive


def test_the_real_sbom_lists_the_compiler_from_the_pinned_lock(real_bundle):
    sigil_root = toolchain.resolve().stdlib_repo
    lock = {(package["name"], package["version"]): package for package in
            tomllib.loads((sigil_root / "Cargo.lock").read_text())["package"]}
    sbom = _sbom_of(real_bundle)
    crates = _crates(sbom)
    names = {name for name, _ in crates}
    assert set(crates) <= set(lock), sorted(set(crates) - set(lock))
    # The compiler, its solver binding, and the Lean kernel's bridge are in it;
    # the other binaries and the test helpers of SIGIL's workspace are not.
    assert {"sigil-mcp", "sigil-compiler", "sigil-formal-bridge", "z3", "z3-sys"} <= names
    assert not names & {"sigil-serve", "sigil-cli", "sigil-test-utils"}, names
    assert len(crates) < len(lock)
    for key, crate in crates.items():
        if "checksum" in lock[key]:
            assert crate["hashes"] == [{"alg": "SHA-256", "content": lock[key]["checksum"]}]
    lean = _properties(_component(sbom, "lean4"))
    assert lean["toolchain"] == (
        sigil_root / "proofs" / "lean" / "lean-toolchain").read_text().strip()


def _free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _boot(root, work):
    """Start the bundle's launcher as an operator does, with the least
    configuration the service accepts. Returns the process, its log, and how
    to ask whether it is ready."""
    work.mkdir(parents=True)
    token = "pin-" + os.urandom(16).hex()
    auth = work / "auth.json"
    now = int(time.time())
    descriptor = os.open(auth, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump({"tokens": [{
            "sha256": hashlib.sha256(token.encode()).hexdigest(),
            "principal": "pin-test", "tenant": "pin-test",
            "scopes": ["chat", "ops:read"], "tools": [],
            "not_before_unix": now - 60, "expires_unix": now + 3600,
        }]}, stream)
    port = _free_port()
    environment = {key: value for key, value in os.environ.items()
                   if key in {"PATH", "LANG", "LC_ALL", "TMPDIR"}}
    environment.update({
        "ANTHROPIC_API_KEY": "pin-test-provider-placeholder",
        # Nothing listens there, and no turn runs: no provider is ever called.
        "PI_ENDPOINT": f"http://127.0.0.1:{_free_port()}/v1/messages",
        "PI_AUDIT_KEY": "pin-test-audit-key-with-at-least-32-bytes",
        "PI_AUTH_FILE": str(auth),
        "PI_STATE": str(work / "state"),
        "PI_HOST": "127.0.0.1",
        "PI_PORT": str(port),
        "PI_SYSTEM_FILE": str(work / "no-system-prompt"),
    })
    log_path = work / "service.log"
    with log_path.open("w") as log:
        process = subprocess.Popen(
            [str(root / "bin" / "sigil-pi")], cwd=root, env=environment,
            stdout=log, stderr=subprocess.STDOUT)
    ready = urllib.request.Request(f"http://127.0.0.1:{port}/v1/ready",
                                   headers={"Authorization": f"Bearer {token}"})
    return process, log_path, ready


def _outcome(process, ready):
    """'exited', 'ready', or 'neither' within STARTUP_WAIT_S. Always stops it."""
    deadline = time.monotonic() + STARTUP_WAIT_S
    outcome = "neither"
    try:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                outcome = "exited"
                break
            try:
                with urllib.request.urlopen(ready, timeout=2) as response:
                    if json.load(response).get("status") == "ready":
                        outcome = "ready"
                        break
            except (OSError, ValueError):
                time.sleep(0.1)
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=60)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
    return outcome


def test_one_changed_byte_in_the_bundled_compiler_stops_startup(real_bundle, tmp_path):
    root = _install(real_bundle, tmp_path / "installed")
    pinned = toolchain.read_rev(root / PIN_FILE).get(
        f"sha256_{toolchain.platform_key()}", "<no pin in the bundle>")
    binary = root / RUNTIME
    data = bytearray(binary.read_bytes())
    data[len(data) // 2] ^= 0x01
    binary.write_bytes(bytes(data))
    process, log_path, ready = _boot(root, tmp_path / "work")
    outcome = _outcome(process, ready)
    log = log_path.read_text()[-3000:]
    assert outcome == "exited" and "forge binary does not match SIGIL_REV" in log, (
        f"startup was not refused for a compiler that differs from the bundle's pin "
        f"(outcome: {outcome}, exit {process.returncode}):\n{log}")
    assert process.returncode != 0
    assert pinned in log and hashlib.sha256(bytes(data)).hexdigest() in log, log


def test_the_bundle_starts_when_its_compiler_matches_its_pin(real_bundle, tmp_path):
    """The control: the same boot of the untouched bundle passes the pin."""
    root = _install(real_bundle, tmp_path / "installed")
    process, log_path, ready = _boot(root, tmp_path / "work")
    outcome = _outcome(process, ready)
    assert outcome == "ready", (
        f"the untouched bundle did not become ready ({outcome}):\n"
        f"{log_path.read_text()[-3000:]}")
