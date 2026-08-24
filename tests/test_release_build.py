"""Release bundles are pinned, complete, and byte-reproducible."""

import json
import subprocess
import tarfile

import pytest

from conftest import PI_ROOT

from scripts.build_release import (
    DOC_FILES,
    PAYLOAD_PARITY_FILES,
    ReleaseBuildError,
    build_release,
    load_candidate_record,
    verify_release,
)
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
        # The release RECORD is not part of the artifact it describes: its
        # in-bundle copy would say "NOT READY — internal alpha" forever, and
        # editing it as evidence lands would move the very digest that
        # evidence binds to.
        assert f"{prefix}/docs/product-readiness.md" not in names
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


# ── the frozen candidate ─────────────────────────────────────────────────
#
# THE BUG CLASS, measured 2026-08-23: product-ci.sh rebuilt the candidate from
# the working tree at gate time, so appending one line to docs/product-readiness.md
# moved the archive digest (a185261e... -> 36dcc981...) — and every collected
# evidence file binds to that digest. Recording a passing result invalidated the
# evidence for it.
#
# Note WHY a partition of DOC_FILES cannot fix this on its own: README.md is in
# the payload and tests/test_guards.py::test_readme_test_count_is_current forces
# it to change whenever a test is added — including the tests below. The
# candidate must stop being a function of the working tree at all.


def _record_for(archive, record_path, pin, **overrides):
    """The candidate record a real release would publish alongside the asset."""
    values = _pin(pin)
    body = {
        "schema_version": 1,
        "version": (PI_ROOT / "VERSION").read_text().strip(),
        "platform_tag": "test-platform",
        "file": archive.name,
        "sha256": _sha256_file(archive),
        "sigil_ref": values["ref"],
        "crates_tree": values["crates"],
        "stdlib_tree": values["stdlib"],
        "rollback_from": {"version": "0.9.0", "sha256": "b" * 64},
    }
    body.update(overrides)
    # Derived AFTER the overrides so a test that substitutes one field gets a
    # record that is otherwise coherent — otherwise it trips an unrelated check
    # and proves nothing about the field under test.
    body.setdefault("tag", "v" + str(body["version"]))
    body.setdefault("release_asset",
                    f"https://github.com/nxrobins/sigil-pi/releases/download/"
                    f"{body['tag']}/{body['file']}")
    record_path.write_text(json.dumps(body, indent=2, sort_keys=True) + "\n")
    return record_path


def _sha256_file(path):
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_verify_release_accepts_the_frozen_candidate(tmp_path):
    sigil, pin = _fake_sigil(tmp_path)
    archive, _ = build_release(
        sigil_root=sigil, output_dir=tmp_path / "dist", app_root=PI_ROOT,
        pin_file=pin, build_runtime=False, platform_tag="test-platform")
    record = load_candidate_record(_record_for(archive, tmp_path / "candidate.json", pin))
    result = verify_release(archive, record=record, app_root=PI_ROOT)
    assert result["sha256"] == _sha256_file(archive)
    assert result["version"] == (PI_ROOT / "VERSION").read_text().strip()


@pytest.mark.parametrize("field,value,match", [
    ("sha256", "c" * 64, "digest"),
    ("file", "sigil-pi-9.9.9-test-platform.tar.gz", "name"),
    ("version", "9.9.9", "VERSION"),
    ("sigil_ref", "d" * 40, "SIGIL"),
    ("crates_tree", "e" * 40, "SIGIL"),
    ("schema_version", 2, "schema"),
])
def test_verify_release_rejects_a_substituted_candidate(tmp_path, field, value, match):
    """The point of a pin is that nothing else satisfies it."""
    sigil, pin = _fake_sigil(tmp_path)
    archive, _ = build_release(
        sigil_root=sigil, output_dir=tmp_path / "dist", app_root=PI_ROOT,
        pin_file=pin, build_runtime=False, platform_tag="test-platform")
    path = _record_for(archive, tmp_path / "candidate.json", pin, **{field: value})
    if field == "schema_version":
        with pytest.raises(ReleaseBuildError, match=match):
            load_candidate_record(path)
        return
    record = load_candidate_record(path)
    with pytest.raises(ReleaseBuildError, match=match):
        verify_release(archive, record=record, app_root=PI_ROOT)


def test_verify_release_detects_a_tampered_archive(tmp_path):
    """A flipped byte must fail the outer digest, and — if someone recomputes
    that — the inner MANIFEST.sha256 as well."""
    sigil, pin = _fake_sigil(tmp_path)
    archive, _ = build_release(
        sigil_root=sigil, output_dir=tmp_path / "dist", app_root=PI_ROOT,
        pin_file=pin, build_runtime=False, platform_tag="test-platform")
    record = load_candidate_record(_record_for(archive, tmp_path / "candidate.json", pin))
    raw = bytearray(archive.read_bytes())
    raw[-1] ^= 0xFF
    archive.write_bytes(bytes(raw))
    with pytest.raises(ReleaseBuildError):
        verify_release(archive, record=record, app_root=PI_ROOT)


def test_candidate_digest_survives_readiness_documentation_edits(tmp_path):
    """THE BUG CLASS, end to end. Recording evidence must not invalidate the
    evidence already recorded."""
    import shutil
    sigil, pin = _fake_sigil(tmp_path)
    app = tmp_path / "app"
    shutil.copytree(PI_ROOT, app, ignore=shutil.ignore_patterns(
        ".git", ".venv", "dist", ".pi-state", "__pycache__", ".pytest_cache"))
    archive, _ = build_release(
        sigil_root=sigil, output_dir=tmp_path / "dist", app_root=app,
        pin_file=pin, build_runtime=False, platform_tag="test-platform")
    record = load_candidate_record(_record_for(archive, tmp_path / "candidate.json", pin))

    # The readiness process does its job: results are recorded.
    (app / "docs" / "product-readiness.md").write_text(
        (app / "docs" / "product-readiness.md").read_text()
        + "\n<!-- load test passed 2026-08-23 -->\n")
    (app / "docs" / "evidence" / "load-test.md").write_text("a qualifying run\n")

    # The frozen candidate is untouched by that, which is the whole point.
    verify_release(archive, record=record, app_root=app)

    # Evicting the record from the payload did half the job: a rebuild after
    # those edits now reproduces the same bytes.
    rebuilt, _ = build_release(
        sigil_root=sigil, output_dir=tmp_path / "dist2", app_root=app,
        pin_file=pin, build_runtime=False, platform_tag="test-platform")
    assert _sha256_file(rebuilt) == record["sha256"]

    # But eviction alone could never be enough, and this is why the gate must
    # VERIFY rather than rebuild: README.md is an operator-facing payload
    # document that tests/test_guards.py::test_readme_test_count_is_current
    # forces to change whenever a test is added — including these tests.
    (app / "README.md").write_text(
        (app / "README.md").read_text() + "\n<!-- the test count moved -->\n")
    moved, _ = build_release(
        sigil_root=sigil, output_dir=tmp_path / "dist3", app_root=app,
        pin_file=pin, build_runtime=False, platform_tag="test-platform")
    assert _sha256_file(moved) != record["sha256"], (
        "a payload document changed, so a rebuild is a DIFFERENT candidate")
    verify_release(archive, record=record, app_root=app)   # the frozen one still verifies


def test_payload_parity_never_covers_a_recording_document():
    """THE GUARD. Parity pins an operator document to the repo copy, so a
    document the readiness process must EDIT can never be parity-checked or
    packaged — that would reinstate the circularity under a new name."""
    recording = {"README.md", "CHANGELOG.md", "SECURITY.md",
                 "docs/product-readiness.md", "docs/capacity.md",
                 "docs/support-matrix.md"}
    assert set(PAYLOAD_PARITY_FILES) & recording == set(), (
        "a document whose content records release status cannot be parity-checked")
    assert "docs/product-readiness.md" not in DOC_FILES, (
        "the release record must not ship inside the artifact it describes")


def test_parity_catches_a_post_freeze_edit_to_an_operator_document(tmp_path):
    """Freezing creates a new hazard: the repo's runbooks could drift from the
    ones an operator holds. Parity turns that into a failed gate rather than a
    3am surprise — but only for documents that INSTRUCT, never those that record."""
    import shutil
    sigil, pin = _fake_sigil(tmp_path)
    app = tmp_path / "app"
    shutil.copytree(PI_ROOT, app, ignore=shutil.ignore_patterns(
        ".git", ".venv", "dist", ".pi-state", "__pycache__", ".pytest_cache"))
    archive, _ = build_release(
        sigil_root=sigil, output_dir=tmp_path / "dist", app_root=app,
        pin_file=pin, build_runtime=False, platform_tag="test-platform")
    record = load_candidate_record(_record_for(archive, tmp_path / "candidate.json", pin))

    (app / "docs" / "capacity.md").write_text("a revised forecast\n")
    verify_release(archive, record=record, app_root=app)   # recording doc: fine

    (app / "docs" / "runbooks.md").write_text("a procedure that no longer matches\n")
    with pytest.raises(ReleaseBuildError, match="payload is stale"):
        verify_release(archive, record=record, app_root=app)


def test_the_record_accepts_the_real_artifact_naming_scheme(tmp_path):
    """Found live, writing the record for the actual published v0.2.0 asset:
    ARCHIVE_NAME_RE lacked `_`, and every Linux artifact is named
    `...-linux-x86_64.tar.gz`. The fixtures never caught it because they used
    platform_tag="test-platform" — no underscore. Validate with the filename
    the release workflow really produces."""
    sigil, pin = _fake_sigil(tmp_path)
    archive, _ = build_release(
        sigil_root=sigil, output_dir=tmp_path / "dist", app_root=PI_ROOT,
        pin_file=pin, build_runtime=False, platform_tag="linux-x86_64")
    assert archive.name.endswith("-linux-x86_64.tar.gz")
    record = load_candidate_record(_record_for(
        archive, tmp_path / "candidate.json", pin,
        platform_tag="linux-x86_64"))
    verify_release(archive, record=record, app_root=PI_ROOT)


def test_a_committed_candidate_record_is_always_loadable():
    """docs/evidence/candidate.json is written by hand from a published asset
    (docs/evidence/README.md documents the ordering), and a hand-written file
    nobody parses until the GA gate is a typo with a long fuse. If the record
    exists, load_candidate_record must accept it — version agreement with the
    tree is deliberately NOT checked here, because between a VERSION bump and
    the next publish the record legitimately names the previous release."""
    committed = PI_ROOT / "docs" / "evidence" / "candidate.json"
    if not committed.is_file():
        pytest.skip("no frozen candidate has been recorded yet")
    record = load_candidate_record(committed)
    assert record["rollback_from"]["version"] != record["version"]
