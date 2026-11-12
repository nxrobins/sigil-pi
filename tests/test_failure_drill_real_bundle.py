"""The failure drill against a real bundle of this tree (audit F-18).

tests/test_failure_drill*.py drive the drill with a stand-in service, which
proves the harness's own logic and nothing about whether it still fits the
product. It stopped fitting once already: when the release began naming its
scratch files uniquely (2026-11-10), the drill's interrupted-write category
could no longer find its boundary, and nothing in the gate ran the drill
against a real release to notice. This module does: it builds a bundle of the
current tree on the pinned toolchain, as tests/test_drill_fixture.py does, and
runs the whole drill against it.

Whatever the host can inject is injected for real: the four categories that
need nothing but the release everywhere, and the interrupted write wherever
the LD_PRELOAD observer can be built (Linux with a C compiler, which includes
CI). The full-disk category is told it has no bounded filesystem, because a
unit test never asks for mount privilege; the dispatch-only `Failure drill`
workflow is where a real filesystem fills.

The run is a rehearsal with a shortened turn deadline and lease, so that the
interrupted write's lease-expiry wait is 31 seconds instead of the shipped
300. That makes it the gate's check of the configuration record too: a real
release must report the two settings as in force, the report must set them
beside what the release ships, and the run must not be eligible to qualify.
"""

import shutil
import sys

import pytest

from conftest import PI_ROOT, needs_toolchain

import toolchain
from scripts import failure_drill
from scripts.build_release import build_release
from scripts.failure_drill import run_drill

REHEARSAL = {"PI_TURN_DEADLINE_SECONDS": "30", "PI_TURN_LEASE_SECONDS": "31"}
EVERYWHERE = ("runtime_crashes", "unavailable_model_providers", "network_failures",
              "corrupt_state")
CAN_INTERRUPT_A_WRITE = sys.platform == "linux" and bool(shutil.which("cc"))


def _no_mount(mount):
    mount.path.mkdir(parents=True, exist_ok=False)
    mount.detail = "a unit test does not mount filesystems"
    return mount


@pytest.fixture(scope="module")
def report(tmp_path_factory):
    needs_toolchain()
    tmp = tmp_path_factory.mktemp("failure-drill")
    archive, _ = build_release(
        sigil_root=toolchain.resolve().stdlib_repo, output_dir=tmp / "bundle",
        app_root=PI_ROOT, build_runtime=False)
    key = tmp / "audit.key"
    key.write_text("failure-drill-real-bundle-key-with-32-plus-bytes\n")
    key.chmod(0o600)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(failure_drill._BoundedFilesystem, "__enter__", _no_mount)
        return run_drill(
            artifact=archive, audit_key_file=key,
            evidence_url="https://example.invalid/failure-drill/real-bundle",
            output=tmp / "failure-injection.json", work_dir=tmp / "work",
            service_settings=REHEARSAL)


def test_a_real_release_reports_the_configuration_the_drill_records(report):
    configuration = report["configuration"]
    in_force = configuration["effective_settings"]
    assert in_force is not None, (
        "the release reported no effective settings: the drill reads them from "
        "/v1/metrics as metrics.effective_settings")
    assert (in_force["turn_deadline_seconds"], in_force["turn_lease_seconds"]) == (30, 31)
    assert in_force["endpoint_host"] == "127.0.0.1"
    assert [entry["setting"] for entry in configuration["overrides"]] == sorted(REHEARSAL)
    for entry in configuration["overrides"]:
        assert entry["in_force"] == int(entry["value"])
        assert type(entry["shipped_default"]) is int
        assert entry["shipped_default"] > int(entry["value"]) and entry["shortened"] is True
    assert configuration["shortened"] == sorted(REHEARSAL)
    assert report["topology"]["production_artifact"] is True


def test_a_rehearsal_with_shortened_timers_is_not_eligible_to_qualify(report):
    assert report["qualification_eligible"] is False
    [shortened] = [failure for failure in report["qualification_failures"]
                   if "shorter than it ships with" in failure]
    for name, value in REHEARSAL.items():
        assert f"{name}={value} (shipped " in shortened


@pytest.mark.parametrize("category", EVERYWHERE)
def test_a_real_fault_lands_on_the_committed_session_and_the_release_keeps_it(
        report, category):
    result = report["categories"][category]
    assert result["passed"] is True, result["failures"]
    assert result["state_integrity_verified"] is True and result["service_recovered"] is True
    fault = result["continuity"]["fault_session"]
    assert fault["session"] == f"{category}-committed"
    assert fault["held_committed_content"] is True and fault["preserved"] is True
    # A real turn commits a user message, a reply, and one audit record per
    # forge; the fault and the recovery turn then grew that same session.
    assert fault["committed_messages"] >= 2 and fault["committed_audit_records"] >= 2
    assert fault["messages_after"] >= fault["committed_messages"] + 2
    assert fault["audit_records_after"] > fault["committed_audit_records"]
    assert result["continuity"]["lost_or_changed"] == []


def test_a_real_refused_start_leaves_the_corrupted_chain_as_it_found_it(report):
    detail = report["categories"]["corrupt_state"]["detail"]
    assert detail["started_over_corruption"] is False
    assert detail["refused_start_left_committed_files_unchanged"] is True
    assert detail["turn_after_repair_status"] == 200


def test_a_real_save_is_interrupted_while_it_replaces_the_committed_conversation(report):
    result = report["categories"]["interrupted_writes"]
    if not CAN_INTERRUPT_A_WRITE:
        # Not a skip: on this host the category must say why it could not run.
        assert result["passed"] is False
        assert result["failures"] == ["write-boundary injection requires Linux and cc"]
        return
    assert result["passed"] is True, result["failures"]
    assert result["state_integrity_verified"] is True
    detail = result["detail"]
    assert detail["boundary_observed"] is True
    assert detail["committed_bytes"] > 0 and detail["temporary_bytes"] > detail["committed_bytes"]
    assert detail["committed_sha256"] != detail["temporary_sha256"]
    assert detail["uncommitted_session_adopted"] is False
    assert detail["scratch_files_after_restart"] == []
    assert detail["crashed_leases"] == 1 and detail["lease_wait_seconds"] <= 36
    fault = result["continuity"]["fault_session"]
    # The interrupted turn left nothing; the turn after the restart left two.
    assert fault["messages_after"] == fault["committed_messages"] + 2


def test_the_full_disk_category_says_it_had_no_filesystem_to_fill(report):
    result = report["categories"]["full_disks"]
    assert result["passed"] is False
    assert result["failures"] == ["full-disk injection skipped: no disposable tmpfs"]
    assert any("could not be bounded" in failure
               for failure in report["qualification_failures"])
