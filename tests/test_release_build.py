"""Release bundles are pinned, complete, and byte-reproducible."""

import json
import subprocess
import tarfile

import pytest

from conftest import PI_ROOT

from scripts.build_release import ReleaseBuildError, build_release
from scripts.build_release import _pin


def _git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def _fake_sigil(tmp_path):
    root = tmp_path / "SIGIL"
    (root / "crates").mkdir(parents=True)
    (root / "crates" / "runtime.txt").write_text("runtime source")
    stdlib = root / "stdlib" / "sigil"
    stdlib.mkdir(parents=True)
    for name in ("http", "json", "kv"):
        (stdlib / f"{name}.sigil").write_text(f"module sigil::{name};\n")
    binary = root / "target" / "release" / "sigil-mcp"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"fake immutable runtime")
    binary.chmod(0o755)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "crates", "stdlib"], check=True)
    subprocess.run([
        "git", "-C", str(root), "-c", "user.name=test", "-c",
        "user.email=test@example.invalid", "commit", "-qm", "fixture"], check=True)
    pin = tmp_path / "SIGIL_REV"
    pin.write_text(
        f"crates = {_git(root, 'rev-parse', 'HEAD:crates')}\n"
        f"stdlib = {_git(root, 'rev-parse', 'HEAD:stdlib')}\n"
        f"ref = {_git(root, 'rev-parse', 'HEAD')}\n")
    return root, pin


def test_release_is_reproducible_and_contains_runtime_contract(tmp_path):
    sigil, pin = _fake_sigil(tmp_path)
    first, first_sum = build_release(
        sigil_root=sigil, output_dir=tmp_path / "one", app_root=PI_ROOT,
        pin_file=pin, build_runtime=False, platform_tag="test-platform")
    second, second_sum = build_release(
        sigil_root=sigil, output_dir=tmp_path / "two", app_root=PI_ROOT,
        pin_file=pin, build_runtime=False, platform_tag="test-platform")
    assert first.read_bytes() == second.read_bytes()
    assert first_sum.read_text().split()[0] == second_sum.read_text().split()[0]
    first_sbom = first.with_name(
        first.name.removesuffix(".tar.gz") + ".SBOM.cdx.json")
    second_sbom = second.with_name(
        second.name.removesuffix(".tar.gz") + ".SBOM.cdx.json")
    assert first_sbom.read_bytes() == second_sbom.read_bytes()
    assert json.loads(first_sbom.read_text())["bomFormat"] == "CycloneDX"

    with tarfile.open(first, "r:gz") as archive:
        names = archive.getnames()
        prefix = names[0].split("/", 1)[0]
        assert f"{prefix}/bin/sigil-pi" in names
        assert f"{prefix}/bin/sigil-pi-release-drill" in names
        assert f"{prefix}/runtime/target/release/sigil-mcp" in names
        assert f"{prefix}/app/product_service.py" in names
        assert f"{prefix}/app/scripts/release_drill.py" in names
        assert f"{prefix}/app/tools/manifest.json" in names
        assert f"{prefix}/docs/capacity.md" in names
        assert f"{prefix}/docs/state-compatibility.md" in names
        assert f"{prefix}/docs/runbooks.md" in names
        assert f"{prefix}/config/alert-policy.json" in names
        assert f"{prefix}/config/grafana-slo-dashboard.json" in names
        assert f"{prefix}/MANIFEST.sha256" in names
        sbom = json.load(archive.extractfile(f"{prefix}/SBOM.cdx.json"))
        assert sbom["bomFormat"] == "CycloneDX"
        assert {c["name"] for c in sbom["components"]} == {"sigil-pi", "SIGIL"}


def test_release_refuses_dirty_or_mismatched_toolchain(tmp_path):
    sigil, pin = _fake_sigil(tmp_path)
    (sigil / "crates" / "runtime.txt").write_text("dirty")
    with pytest.raises(ReleaseBuildError, match="uncommitted"):
        build_release(sigil_root=sigil, output_dir=tmp_path / "out", app_root=PI_ROOT,
                      pin_file=pin, build_runtime=False, platform_tag="test")

    subprocess.run(["git", "-C", str(sigil), "checkout", "--", "crates"], check=True)
    bad_pin = tmp_path / "bad-rev"
    bad_pin.write_text(pin.read_text().replace(
        _git(sigil, "rev-parse", "HEAD:crates"), "0" * 40))
    with pytest.raises(ReleaseBuildError, match="does not match"):
        build_release(sigil_root=sigil, output_dir=tmp_path / "out", app_root=PI_ROOT,
                      pin_file=bad_pin, build_runtime=False, platform_tag="test")


def test_release_rejects_incomplete_pin_and_missing_runtime(tmp_path):
    incomplete = tmp_path / "pin"
    incomplete.write_text("ref = abc\n")
    with pytest.raises(ReleaseBuildError, match="pin file is missing"):
        _pin(incomplete)

    sigil, pin = _fake_sigil(tmp_path)
    (sigil / "target" / "release" / "sigil-mcp").unlink()
    with pytest.raises(ReleaseBuildError, match="not found"):
        build_release(
            sigil_root=sigil, output_dir=tmp_path / "out", app_root=PI_ROOT,
            pin_file=pin, build_runtime=False, platform_tag="test")
