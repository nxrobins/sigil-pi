"""A drill report is evidence only as the bytes its workflow run signed (audit F-19).

`scripts/check_readiness_evidence.py` read the JSON reports committed under
docs/evidence/, checked what they said and that they named the candidate's
digest, and nothing else. A report typed by hand, or a real one edited after
its run, passed as long as it said the right things. The drill workflows now
sign their reports in a job of their own (actions/attest), and the gate asks
GitHub, through `gh attestation verify`, for an attestation of each report's
exact bytes signed by the workflow that runs that drill, in this repository,
from main, on a GitHub-hosted runner, as SLSA provenance.

No test here reaches GitHub. `gh` is replaced by a stand-in that behaves as
gh 2.97 does: GitHub returns the attestations the named repository holds for
the file's sha256 and predicate type; gh keeps those whose signing
certificate meets every policy flag, and prints them as it prints them, or
exits 1. The gate's own reading of what gh printed is tested with a gh that
says yes to anything.
"""

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

import scripts.check_readiness_evidence as gate
from conftest import PI_ROOT
from scripts.check_readiness_evidence import EvidenceError
from test_readiness_evidence import _fixture

REPOSITORY = "nxrobins/sigil-pi"
# Each drill report the gate reads, and the workflow that must have signed it.
DRILL_REPORTS = {
    "failure-injection.json": ".github/workflows/failure-drill.yml",
    "recovery-drill.json": ".github/workflows/recovery-drill.yml",
}
# What actions/attest signs when it is given no predicate.
PROVENANCE = "https://slsa.dev/provenance/v1"


def _attestation(path, workflow, *, repository=REPOSITORY, ref="refs/heads/main",
                 runner="github-hosted", predicate_type=PROVENANCE, run=7001):
    """What GitHub holds once ``workflow`` of ``repository``, run from
    ``ref`` on a ``runner`` runner, signed ``path`` as it is now: the store
    it is in, and the statement and signing identity as gh prints a verified
    attestation (the shape `gh attestation verify --format json` printed for a
    real one, 2026-11-13)."""
    path = Path(path)
    signer = f"https://github.com/{repository}/{workflow}@{ref}"
    return {
        "store": repository,
        "verificationResult": {
            "mediaType": "application/vnd.dev.sigstore.verificationresult+json;version=0.1",
            "statement": {
                "_type": "https://in-toto.io/Statement/v1",
                "subject": [{"name": path.name,
                             "digest": {"sha256": hashlib.sha256(path.read_bytes()).hexdigest()}}],
                "predicateType": predicate_type,
                "predicate": {"buildDefinition": {}, "runDetails": {}},
            },
            "signature": {"certificate": {
                "certificateIssuer": "CN=sigstore-intermediate,O=sigstore.dev",
                "subjectAlternativeName": signer,
                "issuer": "https://token.actions.githubusercontent.com",
                "buildSignerURI": signer,
                "runnerEnvironment": runner,
                "sourceRepositoryURI": f"https://github.com/{repository}",
                "sourceRepositoryRef": ref,
                "runInvocationURI": f"https://github.com/{repository}/actions/runs/{run}/attempts/1",
            }},
            "verifiedTimestamps": [],
        },
    }


def _signed(evidence, **identity):
    """An attestation of each drill report by the workflow that runs it."""
    return [_attestation(evidence / name, workflow, **identity)
            for name, workflow in DRILL_REPORTS.items()]


def _subject(attestation):
    return attestation["verificationResult"]["statement"]["subject"][0]["name"]


class FakeGh:
    """`gh attestation verify` over the attestations it is given, as gh 2.97
    decides: GitHub returns those that ``--repo``'s store holds for the
    file's sha256 and ``--predicate-type``; gh keeps those whose certificate
    matches ``--signer-workflow`` (a regular expression anchored only at its
    start, as gh builds it), names ``--repo`` as its source repository and
    ``--source-ref`` as its ref, and, with ``--deny-self-hosted-runners``,
    was made on a GitHub-hosted runner. Records every command."""

    def __init__(self, *attestations):
        self.attestations = list(attestations)
        self.calls = []

    def __call__(self, command, **options):
        self.calls.append(list(command))
        assert options.get("capture_output") and options.get("text"), options
        assert options.get("timeout"), "gh is run without a time limit"
        assert command[:3] == ["gh", "attestation", "verify"], command
        parser = argparse.ArgumentParser(prog="gh attestation verify")
        parser.add_argument("path")
        parser.add_argument("--repo", required=True)
        parser.add_argument("--signer-workflow")
        parser.add_argument("--source-ref")
        parser.add_argument("--predicate-type", default=PROVENANCE)
        parser.add_argument("--deny-self-hosted-runners", action="store_true")
        parser.add_argument("--format", choices=["json"])
        flags = parser.parse_args(command[3:])
        digest = hashlib.sha256(Path(flags.path).read_bytes()).hexdigest()
        held = [attestation for attestation in self.attestations
                if attestation["store"] == flags.repo
                and attestation["verificationResult"]["statement"]["predicateType"]
                == flags.predicate_type
                and digest in [subject["digest"]["sha256"] for subject in
                               attestation["verificationResult"]["statement"]["subject"]]]
        if not held:
            return subprocess.CompletedProcess(command, 1, "", (
                f"Error: HTTP 404: Not Found (https://api.github.com/repos/{flags.repo}/"
                f"attestations/sha256:{digest}?per_page=30)\n"))

        def meets(certificate):
            return ((flags.signer_workflow is None or re.match(
                        f"^https://github.com/{flags.signer_workflow}",
                        certificate["subjectAlternativeName"]))
                    and certificate["sourceRepositoryURI"] == f"https://github.com/{flags.repo}"
                    and flags.source_ref in (None, certificate["sourceRepositoryRef"])
                    and (not flags.deny_self_hosted_runners
                         or certificate["runnerEnvironment"] == "github-hosted"))

        verified = [{"attestation": {"bundle": {}}, "verificationResult": held_one["verificationResult"]}
                    for held_one in held
                    if meets(held_one["verificationResult"]["signature"]["certificate"])]
        if not verified:
            return subprocess.CompletedProcess(command, 1, "", (
                "Error: verifying with issuer \"sigstore.dev\": no attestation "
                "matched the policy\n"))
        if flags.format == "json":
            return subprocess.CompletedProcess(command, 0, json.dumps(verified), "")
        return subprocess.CompletedProcess(command, 0, "", "✓ Verification succeeded!\n")


@pytest.fixture
def evidence(tmp_path):
    return _fixture(tmp_path)


def _run_gate(evidence, monkeypatch, capsys, gh):
    """The evidence check as product-ci.sh starts it, with ``gh`` standing in
    for every program it runs: its exit status, what it printed on stdout,
    and the lines it wrote on stderr."""
    evidence_dir, artifact, _ = evidence
    monkeypatch.setattr(subprocess, "run", gh)
    monkeypatch.setattr(sys, "argv", [
        "scripts/check_readiness_evidence.py", "--evidence", str(evidence_dir),
        "--artifact", str(artifact), "--version", "1.0.0"])
    try:
        gate.main()
        status = 0
    except SystemExit as stop:
        status = stop.code
    printed, said = capsys.readouterr()
    return status, printed, said.splitlines()


def _refused(status, said, report, reason=""):
    """The gate stopped, and its last word was about ``report``."""
    return (status == 1 and bool(said)
            and said[-1].startswith(f"readiness evidence gate failed: {report}")
            and reason in said[-1])


def test_reports_their_own_workflows_signed_are_evidence(evidence, monkeypatch, capsys):
    evidence_dir, _, signoff = evidence
    gh = FakeGh(*_signed(evidence_dir))
    status, printed, said = _run_gate(evidence, monkeypatch, capsys, gh)
    assert [call[3] for call in gh.calls] == [str(evidence_dir / name) for name in DRILL_REPORTS], (
        "the gate accepted the drill reports without asking GitHub whether their "
        f"workflows signed them: it ran {gh.calls}")
    assert status == 0 and json.loads(printed) == {
        "ready": True, "version": "1.0.0", "artifact_sha256": signoff["artifact_sha256"]}
    assert said == [
        f"attested: {name} (sha256 {hashlib.sha256((evidence_dir / name).read_bytes()).hexdigest()})"
        f" signed by https://github.com/{REPOSITORY}/{workflow}@refs/heads/main in "
        f"https://github.com/{REPOSITORY}/actions/runs/7001/attempts/1"
        for name, workflow in DRILL_REPORTS.items()]


@pytest.mark.parametrize("report", DRILL_REPORTS)
def test_a_report_nobody_signed_is_not_evidence(evidence, monkeypatch, capsys, report):
    """The finding: a report typed by hand, or a real one from a run that
    signed nothing, said the right things and passed."""
    evidence_dir, _, _ = evidence
    gh = FakeGh(*[attestation for attestation in _signed(evidence_dir)
                  if _subject(attestation) != report])
    status, _, said = _run_gate(evidence, monkeypatch, capsys, gh)
    assert _refused(status, said, report, "carries no attestation"), (
        f"the gate took {report}, which nobody signed: exit {status}, {said}")


@pytest.mark.parametrize("report", DRILL_REPORTS)
@pytest.mark.parametrize("workflow", [
    ".github/workflows/release.yml", ".github/workflows/ci.yml", "the other drill's",
    "a lookalike of its own"])
def test_a_report_another_workflow_signed_is_not_evidence(
        evidence, monkeypatch, capsys, report, workflow):
    """Any workflow of the repository can sign any bytes. Only the one that
    runs the drill signing its report says the drill produced it; the
    lookalike (its own name with more after it) is one gh's own match lets
    through, and the gate does not."""
    evidence_dir, _, _ = evidence
    if workflow == "the other drill's":
        [workflow] = [other for name, other in DRILL_REPORTS.items() if name != report]
    elif workflow == "a lookalike of its own":
        workflow = DRILL_REPORTS[report] + ".evil.yml"
    gh = FakeGh(_attestation(evidence_dir / report, workflow),
                *[attestation for attestation in _signed(evidence_dir)
                  if _subject(attestation) != report])
    status, _, said = _run_gate(evidence, monkeypatch, capsys, gh)
    assert _refused(status, said, report), (
        f"the gate took {report} signed by {workflow}: exit {status}, {said}")


@pytest.mark.parametrize("report", DRILL_REPORTS)
@pytest.mark.parametrize("repository", [
    "someone-else/sigil-pi", "nxrobins/sigil", "nxrobins/sigil-pi-rehearsal"])
def test_a_report_another_repository_signed_is_not_evidence(
        evidence, monkeypatch, capsys, report, repository):
    """A fork, or any repository someone controls, can run a copy of the
    drill workflow that writes whatever report it likes and signs it."""
    evidence_dir, _, _ = evidence
    gh = FakeGh(_attestation(evidence_dir / report, DRILL_REPORTS[report], repository=repository),
                *[attestation for attestation in _signed(evidence_dir)
                  if _subject(attestation) != report])
    status, _, said = _run_gate(evidence, monkeypatch, capsys, gh)
    assert _refused(status, said, report, "carries no attestation"), (
        f"the gate took {report} signed in {repository}: exit {status}, {said}")


@pytest.mark.parametrize("report", DRILL_REPORTS)
@pytest.mark.parametrize("edit", ["a value changed", "reformatted", "a newline added"])
def test_a_report_changed_after_its_run_signed_it_is_not_evidence(
        evidence, monkeypatch, capsys, report, edit):
    """The attestation is for the bytes the run wrote. Each edit here keeps
    the report valid, so only the attestation can refuse it."""
    evidence_dir, _, _ = evidence
    gh = FakeGh(*_signed(evidence_dir))
    path = evidence_dir / report
    data = json.loads(path.read_text())
    if edit == "a value changed":
        if report == "recovery-drill.json":
            data["evaluation"]["restore_rto_seconds"] -= 1
        else:
            data["completed_unix"] += 1
        path.write_text(json.dumps(data))
    elif edit == "reformatted":
        path.write_text(json.dumps(data, indent=2))
    else:
        path.write_bytes(path.read_bytes() + b"\n")
    status, _, said = _run_gate(evidence, monkeypatch, capsys, gh)
    assert _refused(status, said, report, "carries no attestation"), (
        f"the gate took {report} with {edit} after its run signed it: exit {status}, {said}")
    assert str(path) in [call[3] for call in gh.calls]


@pytest.mark.parametrize("report", DRILL_REPORTS)
@pytest.mark.parametrize("identity", [
    {"ref": "refs/heads/feature"}, {"ref": "refs/tags/v1.0.0"}, {"ref": "refs/pull/7/merge"},
    {"runner": "self-hosted"}, {"predicate_type": "https://cyclonedx.org/bom"},
    {"predicate_type": "https://in-toto.io/attestation/release/v0.1"},
], ids=lambda identity: "-".join(f"{key}={value}" for key, value in identity.items()))
def test_a_report_signed_off_main_on_a_self_hosted_runner_or_as_another_type_is_not_evidence(
        evidence, monkeypatch, capsys, report, identity):
    """A workflow file on another branch is whatever that branch says it is,
    and a self-hosted runner is whatever its owner made it; neither is the
    drill on a clean host. An SBOM or any other statement about the report
    is not the provenance of it."""
    evidence_dir, _, _ = evidence
    gh = FakeGh(_attestation(evidence_dir / report, DRILL_REPORTS[report], **identity),
                *[attestation for attestation in _signed(evidence_dir)
                  if _subject(attestation) != report])
    status, _, said = _run_gate(evidence, monkeypatch, capsys, gh)
    assert _refused(status, said, report, "carries no attestation"), (
        f"the gate took {report} signed with {identity}: exit {status}, {said}")


def test_the_gate_runs_the_command_the_maintainer_runs(tmp_path, monkeypatch, capsys):
    """product-ci.sh starts the evidence check with exactly these arguments,
    and through them the check runs exactly these two commands, which a
    maintainer can paste into a shell at the repository's root:

        gh attestation verify docs/evidence/failure-injection.json --repo nxrobins/sigil-pi \\
          --signer-workflow nxrobins/sigil-pi/.github/workflows/failure-drill.yml \\
          --source-ref refs/heads/main --predicate-type https://slsa.dev/provenance/v1 \\
          --deny-self-hosted-runners --format json

    and the same for docs/evidence/recovery-drill.json and recovery-drill.yml.
    """
    product_gate = (PI_ROOT / "product-ci.sh").read_text()
    [arguments] = re.findall(
        r"^\.venv/bin/python scripts/check_readiness_evidence\.py \\\n\s+(.+)$",
        product_gate, re.M)
    assert arguments == '--evidence docs/evidence --artifact "$archive" --version "$version"', (
        "product-ci.sh passes the evidence check something more than the evidence, "
        "the artifact and the version")
    (tmp_path / "docs").mkdir()
    evidence_dir, artifact, signoff = _fixture(tmp_path / "docs")
    gh = FakeGh(*_signed(evidence_dir))
    monkeypatch.setattr(subprocess, "run", gh)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", [
        "scripts/check_readiness_evidence.py", "--evidence", "docs/evidence",
        "--artifact", str(artifact), "--version", "1.0.0"])
    gate.main()
    assert gh.calls == [
        ["gh", "attestation", "verify", "docs/evidence/failure-injection.json",
         "--repo", "nxrobins/sigil-pi",
         "--signer-workflow", "nxrobins/sigil-pi/.github/workflows/failure-drill.yml",
         "--source-ref", "refs/heads/main",
         "--predicate-type", "https://slsa.dev/provenance/v1",
         "--deny-self-hosted-runners", "--format", "json"],
        ["gh", "attestation", "verify", "docs/evidence/recovery-drill.json",
         "--repo", "nxrobins/sigil-pi",
         "--signer-workflow", "nxrobins/sigil-pi/.github/workflows/recovery-drill.yml",
         "--source-ref", "refs/heads/main",
         "--predicate-type", "https://slsa.dev/provenance/v1",
         "--deny-self-hosted-runners", "--format", "json"],
    ], f"the gate did not ask gh to verify the drill reports' attestations: it ran {gh.calls}"
    printed, said = capsys.readouterr()
    assert json.loads(printed) == {"ready": True, "version": "1.0.0",
                                   "artifact_sha256": signoff["artifact_sha256"]}
    assert [line.split(" (sha256")[0] for line in said.splitlines()] == [
        "attested: failure-injection.json", "attested: recovery-drill.json"]


def _without_the_documents_people_write(evidence_dir):
    """The evidence right after the drills have run: the security review,
    the load report, the pilot and the sign-offs do not exist yet."""
    for name in ("security-review.md", "security-review.json", "load-test.json",
                 "pilot.md", "release-signoff.json"):
        (evidence_dir / name).unlink()


def test_the_drill_reports_are_checked_before_the_evidence_people_write(
        evidence, monkeypatch, capsys):
    """The gate checks whose the drill reports are, and then what they say,
    before it asks for the documents people write, so the maintainer can see
    it take or refuse the drill reports when the drills have run, and not
    only at the release."""
    evidence_dir, _, _ = evidence
    _without_the_documents_people_write(evidence_dir)
    gh = FakeGh(*_signed(evidence_dir))
    status, _, said = _run_gate(evidence, monkeypatch, capsys, gh)
    assert [call[3] for call in gh.calls] == [str(evidence_dir / name) for name in DRILL_REPORTS]
    assert status == 1 and [line.split(" (sha256")[0] for line in said] == [
        "attested: failure-injection.json", "attested: recovery-drill.json",
        "readiness evidence gate failed: required substantive report is missing: "
        "security-review.md"], said


def test_a_signed_drill_report_that_shows_too_little_is_refused_before_the_documents(
        evidence, monkeypatch, capsys):
    """A run's own signature does not make a report evidence: what it shows
    is still checked, and checked before the documents that do not exist yet."""
    evidence_dir, _, _ = evidence
    _without_the_documents_people_write(evidence_dir)
    path = evidence_dir / "recovery-drill.json"
    report = json.loads(path.read_text())
    del report["phases"][1]["turn"]
    path.write_text(json.dumps(report))
    gh = FakeGh(*_signed(evidence_dir))
    status, _, said = _run_gate(evidence, monkeypatch, capsys, gh)
    assert status == 1 and said[-1] == (
        "readiness evidence gate failed: recovery phase restore_old records no turn"), said


def test_a_missing_drill_report_is_refused_by_name(evidence, monkeypatch, capsys):
    evidence_dir, _, _ = evidence
    gh = FakeGh(*_signed(evidence_dir))
    (evidence_dir / "failure-injection.json").unlink()
    status, _, said = _run_gate(evidence, monkeypatch, capsys, gh)
    assert status == 1 and said == [
        "readiness evidence gate failed: failure-injection.json is missing: there is no "
        "drill report to verify"], said
    assert gh.calls == []


def test_the_evidence_check_has_no_way_to_skip_the_attestations():
    """The gate's arguments are the evidence, the artifact and the version:
    nothing turns the attestation check off, so a maintainer cannot pass one
    by habit and nothing in product-ci.sh can."""
    options = {option for action in gate._arguments()._actions
               for option in action.option_strings}
    assert options == {"-h", "--help", "--evidence", "--artifact", "--version"}


def test_the_gate_expects_the_repository_that_publishes_the_candidate():
    """The repository is the gate's, not the evidence's: a report signed in
    a repository the evidence names could come from anyone's fork. It is the
    one the frozen candidate was published from."""
    assert gate.REPOSITORY == REPOSITORY
    assert gate.ATTESTED_REPORTS == DRILL_REPORTS
    record = json.loads((PI_ROOT / "docs" / "evidence" / "candidate.json").read_text())
    assert record["release_asset"].startswith(f"https://github.com/{REPOSITORY}/releases/download/")
    for workflow in DRILL_REPORTS.values():
        assert (PI_ROOT / workflow).is_file(), workflow


def test_the_gate_and_the_drill_name_the_same_measures():
    """The validator restates what the recovery drill records, and shares no
    code with it; this keeps the two in step."""
    from scripts import release_drill
    assert gate.RECOVERY_MEASURED_TO == release_drill.MEASURED_TO
    assert gate.RECOVERY_ONLINE_PHASES == tuple(release_drill.ONLINE_PHASES)


# ── what the gate itself checks in what gh printed ──

def _says_yes(printed):
    """A gh that exits 0 and prints ``printed``, whatever it is asked."""
    def run(command, **options):
        return subprocess.CompletedProcess(command, 0, printed, "")
    return run


def _report(tmp_path):
    path = tmp_path / "recovery-drill.json"
    path.write_text('{"test": "sigil-pi-v1-release-recovery"}')
    return path


def _verified(path, **identity):
    return {"attestation": {"bundle": {}}, "verificationResult": _attestation(
        path, DRILL_REPORTS["recovery-drill.json"], **identity)["verificationResult"]}


def _certificate(result):
    return result["verificationResult"]["signature"]["certificate"]


def test_the_gate_reads_what_gh_verified(tmp_path):
    path = _report(tmp_path)
    attested = gate.verify_attestation(
        path, ".github/workflows/recovery-drill.yml",
        run=_says_yes(json.dumps([_verified(path, run=31), _verified(path, run=32)])))
    assert attested == {
        "report": "recovery-drill.json",
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "signer": "https://github.com/nxrobins/sigil-pi/.github/workflows/recovery-drill.yml"
                  "@refs/heads/main",
        "runs": ["https://github.com/nxrobins/sigil-pi/actions/runs/31/attempts/1",
                 "https://github.com/nxrobins/sigil-pi/actions/runs/32/attempts/1"],
    }


@pytest.mark.parametrize("change,match", [
    (lambda result, _: _certificate(result).update(
        subjectAlternativeName="https://github.com/nxrobins/sigil-pi/.github/workflows/"
                               "recovery-drill.yml.evil.yml@refs/heads/main"),
     "not signed by"),
    (lambda result, _: _certificate(result).update(
        subjectAlternativeName="https://github.com/nxrobins/sigil-pi/.github/workflows/"
                               "recovery-drill.yml@refs/heads/feature"),
     "not signed by"),
    (lambda result, _: _certificate(result).update(
        sourceRepositoryURI="https://github.com/someone-else/sigil-pi"), "not signed by"),
    (lambda result, _: _certificate(result).update(sourceRepositoryRef="refs/heads/feature"),
     "not signed by"),
    (lambda result, _: _certificate(result).update(runnerEnvironment="self-hosted"),
     "not signed by"),
    (lambda result, _: _certificate(result).pop("runnerEnvironment"), "not signed by"),
    (lambda result, _: _certificate(result).update(
        runInvocationURI="https://github.com/someone-else/sigil-pi/actions/runs/1/attempts/1"),
     "not signed by"),
    (lambda result, _: _certificate(result).pop("runInvocationURI"), "not signed by"),
    (lambda result, _: result["verificationResult"]["statement"]["subject"][0].update(
        digest={"sha256": "0" * 64}), "for other bytes"),
    (lambda result, _: result["verificationResult"]["statement"].update(subject=[]),
     "for other bytes"),
    (lambda result, _: result["verificationResult"]["statement"].update(
        predicateType="https://cyclonedx.org/bom"), "of type"),
    (lambda result, _: result["verificationResult"].pop("signature"),
     "printed no verified attestation"),
    (lambda result, _: result.pop("verificationResult"), "printed no verified attestation"),
], ids=["a lookalike workflow", "a branch in the signer", "another source repository",
        "another ref", "a self-hosted runner", "no runner", "another repository's run",
        "no run", "other bytes", "no subject", "another predicate", "no signature",
        "no result"])
def test_the_gate_does_not_take_gh_s_word_for_the_identity(tmp_path, change, match):
    """gh matches the signer workflow as a prefix, and every other policy is
    a flag the gate passes; the gate also reads what gh says it verified and
    refuses any attestation that is not exactly the expected one, so a change
    in gh, or a gh that says yes, cannot let another report through."""
    path = _report(tmp_path)
    good, bad = _verified(path), _verified(path)
    change(bad, path)
    with pytest.raises(EvidenceError, match=f"^recovery-drill.json: .*{match}"):
        gate.verify_attestation(path, ".github/workflows/recovery-drill.yml",
                                run=_says_yes(json.dumps([good, bad])))


@pytest.mark.parametrize("printed", ["", "✓ Verification succeeded!\n", "[]", "{}", "[1]", "null"])
def test_a_gh_that_verified_nothing_it_can_show_is_refused(tmp_path, printed):
    with pytest.raises(EvidenceError, match="^recovery-drill.json: .*printed no verified attestation"):
        gate.verify_attestation(_report(tmp_path), ".github/workflows/recovery-drill.yml",
                                run=_says_yes(printed))


def test_without_gh_the_gate_refuses(tmp_path):
    def missing(command, **options):
        raise FileNotFoundError(2, "No such file or directory", command[0])
    with pytest.raises(EvidenceError, match="^recovery-drill.json cannot be checked: gh"):
        gate.verify_attestation(_report(tmp_path), ".github/workflows/recovery-drill.yml",
                                run=missing)


def test_a_gh_that_does_not_answer_is_refused(tmp_path):
    def hangs(command, **options):
        raise subprocess.TimeoutExpired(command, options["timeout"])
    with pytest.raises(EvidenceError, match="^recovery-drill.json: gh attestation verify did not finish"):
        gate.verify_attestation(_report(tmp_path), ".github/workflows/recovery-drill.yml",
                                run=hangs)


def test_what_gh_said_when_it_refused_is_kept_short(tmp_path):
    """gh's own reason reaches the maintainer, but a long or multi-line
    answer does not flood the gate's one line."""
    def refuses(command, **options):
        return subprocess.CompletedProcess(command, 1, "", "line one\n" + "x" * 5000 + "\nlast line\n")
    with pytest.raises(EvidenceError) as refused:
        gate.verify_attestation(_report(tmp_path), ".github/workflows/recovery-drill.yml",
                                run=refuses)
    message = str(refused.value)
    assert "\n" not in message and len(message) < 800 and message.endswith("last line)")
