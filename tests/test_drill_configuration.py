"""The recovery drill and its fixture boot releases as they ship (audit F-18).

The failure drill was found starting the release with a 60-second lease (it
ships 300), 30-second turn and compiler deadlines (120 and 90) and a 30-second
drain (120), and recording none of it. `scripts/release_drill.py` and
`scripts/drill_fixture.py` passed the same shortened timers to every release
they booted, and their reports were as silent. They now leave the timers to
the release, and record the few settings they still supply.

tests/test_failure_drill_integrity.py holds the failure drill's own tests.
"""

import re
import subprocess
from types import SimpleNamespace

import pytest

from conftest import PI_ROOT
from scripts import drill_fixture, release_drill
from scripts.drill_fixture import DrillFixtureError, produce_backup
from scripts.release_drill import ReleaseDrillError, _probe_release, run_drill
from test_failure_drill import AUDIT_KEY, _artifact, _audit_key_file
from test_release_drill import _artifacts, _backup, _injected_probe, _stand_ins

BUDGET_SUFFIXES = ("_SECONDS", "_DAYS", "_RETRIES")
# What a drill has to supply for a release to start at all, and the one
# setting that removes the system prompt (recorded as `system_prompt`).
WIRING = {"ANTHROPIC_API_KEY", "PI_AUDIT_KEY", "PI_AUTH_FILE", "PI_ENDPOINT",
          "PI_HOST", "PI_PORT", "PI_STATE", "PI_SYSTEM_FILE"}
PROCESS_PLUMBING = {"PATH", "LANG", "LC_ALL", "TMPDIR", "PYTHONPATH"}


def _shipped_budgets():
    """Every timer and retry budget product_main.py reads with an integer
    default, as {variable: shipped value}."""
    source = (PI_ROOT / "product_main.py").read_text()
    shipped = {name: int(default) for name, default in re.findall(
        r'_env_int\(\s*"(PI_[A-Z_]+)",\s*(\d+)\s*\)', source)}
    return {name: value for name, value in shipped.items()
            if name.endswith(BUDGET_SUFFIXES)}


def _spawned_environments(monkeypatch):
    """Stand in for the release process: record what each one is started
    with, and exit at once so the caller gives up cleanly."""
    spawned = []

    def popen(command, **kwargs):
        spawned.append(dict(kwargs["env"]))
        return SimpleNamespace(poll=lambda: 1, returncode=1, pid=1)

    monkeypatch.setattr(subprocess, "Popen", popen)
    return spawned


def _probe_environment(tmp_path, monkeypatch):
    spawned = _spawned_environments(monkeypatch)
    with pytest.raises(ReleaseDrillError, match="did not become ready"):
        _probe_release(tmp_path / "release", tmp_path / "state", tmp_path / "auth.json",
                       "token", AUDIT_KEY, endpoint="http://127.0.0.1:9/v1/messages",
                       session_token="session-token", session="a-session",
                       message="a message")
    [environment] = spawned
    return environment


def _fixture_environment(tmp_path, monkeypatch):
    artifact, key = _artifact(tmp_path), _audit_key_file(tmp_path)
    spawned = _spawned_environments(monkeypatch)
    with pytest.raises(DrillFixtureError, match="exited during startup"):
        produce_backup(artifact=artifact, output=tmp_path / "backup.tar.gz",
                       audit_key_file=key, work_dir=tmp_path / "work")
    [environment] = spawned
    return environment


ENVIRONMENTS = {"recovery drill": _probe_environment, "drill fixture": _fixture_environment}


def test_the_shipped_timers_can_be_read_from_the_product():
    """Anti-vacuity for the comparison below: it has shipped values to
    compare with, among them the four the drills used to shorten."""
    shipped = _shipped_budgets()
    assert {"PI_DRAIN_TIMEOUT_SECONDS", "PI_TURN_DEADLINE_SECONDS",
            "PI_TURN_LEASE_SECONDS", "PI_MCP_TIMEOUT_SECONDS", "PI_LLM_RETRIES",
            *release_drill.SERVICE_SETTINGS} <= set(shipped), sorted(shipped)


@pytest.mark.parametrize("drill", sorted(ENVIRONMENTS))
def test_a_release_is_started_with_no_timer_shorter_than_it_ships_with(
        tmp_path, monkeypatch, drill):
    environment = ENVIRONMENTS[drill](tmp_path, monkeypatch)
    shipped = _shipped_budgets()
    budgets = {name: value for name, value in environment.items()
               if name.startswith("PI_") and name.endswith(BUDGET_SUFFIXES)}
    unknown = sorted(set(budgets) - set(shipped))
    assert not unknown, f"no shipped value found in product_main.py for {unknown}"
    shortened = {name: f"{value} (shipped {shipped[name]})"
                 for name, value in budgets.items() if int(value) < shipped[name]}
    assert not shortened, (
        f"the {drill} starts each release with {shortened}: shorter than the "
        f"release ships with, and nowhere in its report (audit F-18)")
    assert budgets == release_drill.SERVICE_SETTINGS


@pytest.mark.parametrize("drill", sorted(ENVIRONMENTS))
def test_everything_a_release_is_started_with_is_wiring_or_recorded(
        tmp_path, monkeypatch, drill):
    """The class guard: a setting added to either environment without a line
    in the reports fails here."""
    environment = ENVIRONMENTS[drill](tmp_path, monkeypatch)
    recorded = release_drill.service_configuration()
    unrecorded = sorted(set(environment) - PROCESS_PLUMBING - WIRING
                        - set(recorded["settings"]))
    assert not unrecorded, (
        f"the {drill} starts each release with {unrecorded}, which no report records")
    assert {name: environment[name] for name in recorded["settings"]} == recorded["settings"]
    assert "PI_SYSTEM_FILE" in environment and recorded["system_prompt"].startswith("none")


def test_the_recovery_report_records_the_settings_it_gave_each_release(tmp_path, monkeypatch):
    _stand_ins(monkeypatch)
    old, new = _artifacts(tmp_path)
    backup, key, now = _backup(tmp_path)
    report = run_drill(
        old_artifact=old, new_artifact=new, state_backup=backup, audit_key_file=key,
        output=tmp_path / "report.json", work_dir=tmp_path / "work",
        probe=_injected_probe, now_fn=lambda: now)
    environment = _probe_environment(tmp_path / "probe", monkeypatch)
    given = {name: value for name, value in environment.items()
             if name not in PROCESS_PLUMBING | WIRING}
    assert report.get("configuration", {}).get("settings") == given, (
        f"the recovery drill gives each release {given} and its report records "
        f"{report.get('configuration')} (audit F-18)")


def test_the_waits_cover_the_timers_a_release_ships_with():
    """A harness that hung up on a turn after 45 seconds, or killed a drain
    after 35, would fail a release for being configured as shipped."""
    shipped = _shipped_budgets()
    assert release_drill.DRAIN_WAIT_SECONDS > shipped["PI_DRAIN_TIMEOUT_SECONDS"]
    assert release_drill.TURN_WAIT_SECONDS > shipped["PI_TURN_DEADLINE_SECONDS"]
    assert drill_fixture.TURN_WAIT_SECONDS > shipped["PI_TURN_DEADLINE_SECONDS"]
