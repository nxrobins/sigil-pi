#!/usr/bin/env python3
"""Run a digest-bound clean-install, restore, upgrade, and rollback drill."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path, PurePosixPath


REPORT_SCHEMA = 1
MAX_ARTIFACT_BYTES = 20 * 1024 * 1024 * 1024
MAX_MEMBERS = 100_000
DEFAULT_PROBE_TIMEOUT = 60


class ReleaseDrillError(RuntimeError):
    pass


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_relative(value):
    path = PurePosixPath(value)
    if (not value or path.is_absolute() or not path.parts
            or any(part in ("", ".", "..") for part in path.parts)):
        raise ReleaseDrillError(f"unsafe archive path: {value!r}")
    return path


def _verify_manifest(root):
    manifest = root / "MANIFEST.sha256"
    if not manifest.is_file() or manifest.is_symlink():
        raise ReleaseDrillError("release MANIFEST.sha256 is missing")
    declared = {}
    for line in manifest.read_text().splitlines():
        digest, separator, relative = line.partition("  ")
        if (not separator or not re.fullmatch(r"[0-9a-f]{64}", digest)
                or relative in declared):
            raise ReleaseDrillError("release manifest has an invalid or repeated entry")
        _safe_relative(relative)
        declared[relative] = digest
    actual = {
        path.relative_to(root).as_posix(): path
        for path in root.rglob("*")
        if path.is_file() and path != manifest
    }
    if set(actual) != set(declared):
        raise ReleaseDrillError("release manifest does not exactly cover installed files")
    for relative, path in actual.items():
        if _sha256(path) != declared[relative]:
            raise ReleaseDrillError(f"release manifest checksum failed: {relative}")


def _install_artifact(artifact, releases_dir, role):
    artifact = Path(artifact)
    releases_dir = Path(releases_dir)
    if artifact.is_symlink() or not artifact.is_file():
        raise ReleaseDrillError(f"{role} artifact is not a regular file")
    digest = _sha256(artifact)
    releases_dir.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{role}-", dir=releases_dir))
    try:
        with tarfile.open(artifact, "r:gz") as archive:
            members = archive.getmembers()
            if len(members) > MAX_MEMBERS:
                raise ReleaseDrillError("release artifact contains too many members")
            seen = set()
            roots = set()
            total = 0
            for member in members:
                relative = _safe_relative(member.name.rstrip("/"))
                normalized = relative.as_posix()
                if normalized in seen:
                    raise ReleaseDrillError(f"release artifact repeats a path: {normalized}")
                seen.add(normalized)
                roots.add(relative.parts[0])
                if not (member.isdir() or member.isreg()):
                    raise ReleaseDrillError(
                        f"release artifact contains a link or special file: {normalized}")
                total += member.size
                if member.size < 0 or total > MAX_ARTIFACT_BYTES:
                    raise ReleaseDrillError("release artifact exceeds the extraction limit")
            if len(roots) != 1:
                raise ReleaseDrillError("release artifact must contain one top-level directory")
            for member in members:
                relative = _safe_relative(member.name.rstrip("/"))
                destination = staging.joinpath(*relative.parts)
                if member.isdir():
                    destination.mkdir(parents=True, exist_ok=True, mode=0o755)
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
                source = archive.extractfile(member)
                if source is None:
                    raise ReleaseDrillError(f"release member cannot be read: {relative}")
                with destination.open("wb") as output:
                    shutil.copyfileobj(source, output)
                destination.chmod(0o755 if member.mode & 0o111 else 0o644)
        [top_name] = roots
        extracted = staging / top_name
        _verify_manifest(extracted)
        required = (
            "app/VERSION", "app/state_tool.py", "bin/sigil-pi",
            "runtime/target/release/sigil-mcp")
        if any(not (extracted / relative).is_file() for relative in required):
            raise ReleaseDrillError("release artifact lacks a required runtime file")
        version = (extracted / "app" / "VERSION").read_text().strip()
        if not version:
            raise ReleaseDrillError("installed release has an empty VERSION")
        destination = releases_dir / f"{role}-{digest[:12]}"
        if destination.exists() or destination.is_symlink():
            raise ReleaseDrillError(f"release install target already exists: {destination}")
        os.replace(extracted, destination)
        return {"root": destination, "sha256": digest, "version": version}
    except (OSError, tarfile.TarError) as error:
        raise ReleaseDrillError(f"cannot install {role} artifact: {error}") from error
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _switch_current(current, target):
    current, target = Path(current), Path(target).resolve()
    current.parent.mkdir(parents=True, exist_ok=True)
    temporary = current.with_name(f".{current.name}.{os.getpid()}.tmp")
    temporary.unlink(missing_ok=True)
    try:
        os.symlink(target, temporary)
        os.replace(temporary, current)
    finally:
        temporary.unlink(missing_ok=True)
    if current.resolve(strict=True) != target:
        raise ReleaseDrillError("atomic release switch did not select the expected target")


def _read_private_key(path):
    path = Path(path)
    try:
        info = path.lstat()
    except OSError as error:
        raise ReleaseDrillError("audit key file is missing") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ReleaseDrillError("audit key must be a non-symlink regular file")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise ReleaseDrillError("audit key file must not be accessible by group or other")
    try:
        value = path.read_text().strip()
    except (OSError, UnicodeDecodeError) as error:
        raise ReleaseDrillError("audit key file must contain UTF-8 text") from error
    if len(value.encode()) < 32 or "\x00" in value or "\n" in value:
        raise ReleaseDrillError("audit key must be one line with at least 32 bytes")
    return value


def _backup_payloads(backup):
    backup = Path(backup)
    if backup.is_symlink() or not backup.is_file():
        raise ReleaseDrillError("state backup is not a regular file")
    try:
        with tarfile.open(backup, "r:gz") as archive:
            members = {member.name: member for member in archive.getmembers()}
            manifest_member = members.get("manifest.json")
            if manifest_member is None or not manifest_member.isreg():
                raise ReleaseDrillError("state backup has no regular manifest")
            manifest = json.load(archive.extractfile(manifest_member))
            if manifest.get("schema_version") != 1:
                raise ReleaseDrillError("state backup schema is not supported by this drill")
            entries = manifest.get("files")
            if not isinstance(entries, list):
                raise ReleaseDrillError("state backup manifest file list is invalid")
            payloads = {}
            for entry in entries:
                if not isinstance(entry, dict):
                    raise ReleaseDrillError("state backup manifest entry is invalid")
                relative = _safe_relative(entry.get("path", "")).as_posix()
                if relative in payloads:
                    raise ReleaseDrillError("state backup manifest repeats a path")
                member = members.get(f"data/{relative}")
                if member is None or not member.isreg() or member.size != entry.get("size"):
                    raise ReleaseDrillError(f"state backup member is missing: {relative}")
                data = archive.extractfile(member).read()
                if hashlib.sha256(data).hexdigest() != entry.get("sha256"):
                    raise ReleaseDrillError(f"state backup checksum failed: {relative}")
                payloads[relative] = data
            return manifest, payloads
    except (OSError, tarfile.TarError, json.JSONDecodeError) as error:
        raise ReleaseDrillError(f"cannot read state backup: {error}") from error


def _run_state_tool(release_root, command, *, audit_key, backup, state):
    script = Path(release_root) / "app" / "state_tool.py"
    args = [sys.executable, str(script), command]
    if command == "restore":
        args.extend(["--backup", str(backup), "--state", str(state)])
    else:
        args.extend(["--state", str(state), "--output", str(backup)])
    environment = dict(os.environ)
    environment["PI_AUDIT_KEY"] = audit_key
    result = subprocess.run(
        args, cwd=script.parent, env=environment, capture_output=True, text=True)
    if result.returncode:
        diagnostic = (result.stderr or result.stdout).strip()[-2000:]
        raise ReleaseDrillError(f"state {command} failed: {diagnostic}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise ReleaseDrillError(f"state {command} returned invalid evidence") from error


def _free_loopback_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def _request_json(url, token):
    request = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "X-Request-ID": "release-drill-probe",
    })
    with urllib.request.urlopen(request, timeout=2) as response:
        return response.status, json.load(response)


def _probe_release(release_root, state_dir, auth_file, token, audit_key,
                   timeout_s=DEFAULT_PROBE_TIMEOUT):
    release_root, state_dir = Path(release_root), Path(state_dir)
    port = _free_loopback_port()
    environment = {
        key: value for key, value in os.environ.items()
        if key in {"PATH", "LANG", "LC_ALL", "TMPDIR", "PYTHONPATH"}
    }
    environment.update({
        "ANTHROPIC_API_KEY": "release-drill-provider-placeholder",
        "PI_AUDIT_KEY": audit_key,
        "PI_AUTH_FILE": str(auth_file),
        "PI_STATE": str(state_dir),
        "PI_HOST": "127.0.0.1",
        "PI_PORT": str(port),
        "PI_DRAIN_TIMEOUT_SECONDS": "30",
        "PI_TURN_DEADLINE_SECONDS": "30",
        "PI_TURN_LEASE_SECONDS": "60",
        "PI_MCP_TIMEOUT_SECONDS": "30",
        "PI_AUDIT_VERIFY_SECONDS": "3600",
        "PI_RETENTION_DAYS": "365000",
        "PI_RETENTION_INTERVAL_SECONDS": "3600",
        "PI_SYSTEM_FILE": str(state_dir.parent / "no-system-prompt"),
    })
    log = tempfile.TemporaryFile(mode="w+t")
    process = subprocess.Popen(
        [str(release_root / "bin" / "sigil-pi")], cwd=release_root,
        env=environment, stdout=log, stderr=log, text=True)
    started = time.monotonic()
    ready_at = None
    version_body = None
    try:
        deadline = started + timeout_s
        while time.monotonic() < deadline:
            if process.poll() is not None:
                break
            try:
                status, ready = _request_json(
                    f"http://127.0.0.1:{port}/v1/ready", token)
                if status == 200 and ready.get("status") == "ready":
                    ready_at = time.monotonic()
                    _, version_body = _request_json(
                        f"http://127.0.0.1:{port}/v1/version", token)
                    _request_json(f"http://127.0.0.1:{port}/v1/metrics", token)
                    break
            except (OSError, ValueError, urllib.error.URLError, json.JSONDecodeError):
                time.sleep(0.05)
        if ready_at is None:
            log.seek(0)
            raise ReleaseDrillError(
                "release did not become ready: " + log.read()[-3000:])
        expected = (release_root / "app" / "VERSION").read_text().strip()
        if version_body.get("version") != expected:
            raise ReleaseDrillError("version endpoint does not match installed VERSION")
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
            try:
                process.wait(timeout=35)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        log.seek(0)
        diagnostic = log.read()[-3000:]
        log.close()
    if process.returncode != 0:
        raise ReleaseDrillError(f"release did not shut down cleanly: {diagnostic}")
    marker = state_dir / ".product-clean-shutdown.json"
    if not marker.is_file():
        raise ReleaseDrillError("release probe produced no clean-shutdown marker")
    return {
        "version": version_body["version"],
        "ready_seconds": round(ready_at - started, 6),
        "total_seconds": round(time.monotonic() - started, 6),
    }


def _write_auth(path, token, now):
    document = {"tokens": [{
        "sha256": hashlib.sha256(token.encode()).hexdigest(),
        "principal": "release-drill-operator",
        "tenant": "release-drill",
        "scopes": ["ops:read"],
        "tools": [],
        "not_before_unix": int(now) - 60,
        "expires_unix": int(now) + 3600,
    }]}
    path.write_text(json.dumps(document, sort_keys=True, separators=(",", ":")))
    path.chmod(0o600)


def _customer_continuity(before, after):
    prefixes = ("sessions/", "sandboxes/", "audit/")
    critical = {path: data for path, data in before.items() if path.startswith(prefixes)}
    lost = sorted(path for path, data in critical.items() if after.get(path) != data)
    return {
        "committed_customer_files": len(critical),
        "committed_session_files": sum(path.startswith("sessions/") for path in critical),
        "lost_or_changed": lost,
        "preserved": not lost,
    }


def run_drill(*, old_artifact, new_artifact, state_backup, audit_key_file,
              output, work_dir=None, max_backup_age_s=900,
              max_restore_s=4 * 3600, max_rollback_s=15 * 60,
              probe=None, now_fn=time.time):
    if min(max_backup_age_s, max_restore_s, max_rollback_s) <= 0:
        raise ReleaseDrillError("all drill thresholds must be positive")
    output = Path(output)
    if output.exists() or output.is_symlink():
        raise ReleaseDrillError("refusing to overwrite an existing drill report")
    audit_key = _read_private_key(audit_key_file)
    started_unix = now_fn()
    owned = work_dir is None
    root = Path(tempfile.mkdtemp(prefix="sigil-pi-release-drill-")) if owned else Path(work_dir)
    if not owned:
        if root.exists():
            raise ReleaseDrillError("work directory must not already exist")
        root.mkdir(parents=True)
    phases = []
    actual_probe = probe is None
    probe_fn = probe or _probe_release
    try:
        releases = root / "releases"
        old = _install_artifact(old_artifact, releases, "old")
        new = _install_artifact(new_artifact, releases, "new")
        current = root / "current"
        token = secrets.token_urlsafe(32)
        auth_file = root / "auth.json"
        _write_auth(auth_file, token, started_unix)

        def execute(name, installed, state):
            phase_started = time.monotonic()
            _switch_current(current, installed["root"])
            result = probe_fn(current.resolve(), state, auth_file, token, audit_key)
            if result.get("version") != installed["version"]:
                raise ReleaseDrillError(f"{name} probe returned the wrong version")
            result = dict(result)
            result.update({
                "name": name, "artifact_sha256": installed["sha256"],
                "elapsed_seconds": round(time.monotonic() - phase_started, 6),
            })
            phases.append(result)
            return result

        execute("clean_install", old, root / "clean-state")
        backup_manifest, before_payloads = _backup_payloads(state_backup)
        created = backup_manifest.get("created_unix")
        if not isinstance(created, int):
            raise ReleaseDrillError("state backup lacks an integer creation time")
        backup_age = started_unix - created
        if backup_age < -60:
            raise ReleaseDrillError("state backup creation time is in the future")

        restored_state = root / "restored-state"
        restore_started = time.monotonic()
        _run_state_tool(
            old["root"], "restore", audit_key=audit_key,
            backup=state_backup, state=restored_state)
        restore_probe = execute("restore_old", old, restored_state)
        restore_seconds = round(
            time.monotonic() - restore_started
            - (restore_probe.get("total_seconds", 0)
               - restore_probe.get("ready_seconds", 0)), 6)
        execute("upgrade_new", new, restored_state)
        rollback_started = time.monotonic()
        rollback_probe = execute("rollback_old", old, restored_state)
        rollback_seconds = round(
            time.monotonic() - rollback_started
            - (rollback_probe.get("total_seconds", 0)
               - rollback_probe.get("ready_seconds", 0)), 6)

        post_backup = root / "post-rollback-state.tar.gz"
        _run_state_tool(
            old["root"], "backup", audit_key=audit_key,
            backup=post_backup, state=restored_state)
        _, after_payloads = _backup_payloads(post_backup)
        continuity = _customer_continuity(before_payloads, after_payloads)
        mechanics_failures = []
        if not continuity["preserved"]:
            mechanics_failures.append("committed customer state changed or disappeared")
        if restore_seconds > max_restore_s:
            mechanics_failures.append("restore/RTO exceeded the configured threshold")
        if rollback_seconds > max_rollback_s:
            mechanics_failures.append("rollback exceeded the configured threshold")
        qualification_failures = list(mechanics_failures)
        if not actual_probe:
            qualification_failures.append("probe was injected rather than the real service")
        if old["sha256"] == new["sha256"] or old["version"] == new["version"]:
            qualification_failures.append("old and new releases are not distinct versions")
        if backup_age < 0 or backup_age > max_backup_age_s:
            qualification_failures.append("input backup does not demonstrate the RPO threshold")
        if continuity["committed_session_files"] == 0:
            qualification_failures.append("backup contains no committed session anti-vacuity fixture")
        report = {
            "schema_version": REPORT_SCHEMA,
            "test": "sigil-pi-v1-release-recovery",
            "started_unix": int(started_unix),
            "completed_unix": int(now_fn()),
            "qualification_eligible": not qualification_failures,
            "qualification_failures": qualification_failures,
            "artifacts": {
                "old": {"version": old["version"], "sha256": old["sha256"]},
                "new": {"version": new["version"], "sha256": new["sha256"]},
            },
            "backup": {
                "sha256": _sha256(state_backup),
                "schema_version": backup_manifest["schema_version"],
                "product_version": backup_manifest.get("product_version"),
                "created_unix": created,
                "age_seconds": round(backup_age, 6),
            },
            "topology": {
                "workers": 1,
                "state_filesystem": "local-posix",
                "platform": platform.platform(),
                "python": platform.python_version(),
                "real_service_probe": actual_probe,
            },
            "thresholds": {
                "maximum_backup_age_seconds": max_backup_age_s,
                "maximum_restore_seconds": max_restore_s,
                "maximum_rollback_seconds": max_rollback_s,
            },
            "phases": phases,
            "evaluation": {
                "mechanics_passed": not mechanics_failures,
                "failures": mechanics_failures,
                "restore_rto_seconds": restore_seconds,
                "rollback_seconds": rollback_seconds,
                "continuity": continuity,
            },
        }
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_name(f".{output.name}.{os.getpid()}.tmp")
        try:
            temporary.write_text(
                json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n")
            temporary.chmod(0o600)
            os.replace(temporary, output)
        finally:
            temporary.unlink(missing_ok=True)
        return report
    finally:
        if owned:
            shutil.rmtree(root, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old-artifact", required=True)
    parser.add_argument("--new-artifact", required=True)
    parser.add_argument("--state-backup", required=True)
    parser.add_argument("--audit-key-file", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--work-dir")
    parser.add_argument("--max-backup-age-seconds", type=float, default=900)
    parser.add_argument("--max-restore-seconds", type=float, default=4 * 3600)
    parser.add_argument("--max-rollback-seconds", type=float, default=15 * 60)
    parser.add_argument(
        "--mechanics-only", action="store_true",
        help="write a non-qualifying report instead of failing when artifacts/backup are fixtures")
    args = parser.parse_args()
    try:
        report = run_drill(
            old_artifact=args.old_artifact,
            new_artifact=args.new_artifact,
            state_backup=args.state_backup,
            audit_key_file=args.audit_key_file,
            output=args.output,
            work_dir=args.work_dir,
            max_backup_age_s=args.max_backup_age_seconds,
            max_restore_s=args.max_restore_seconds,
            max_rollback_s=args.max_rollback_seconds)
    except (OSError, ReleaseDrillError) as error:
        parser.exit(2, f"release drill failed: {error}\n")
    print(json.dumps(report, sort_keys=True))
    if not report["qualification_eligible"] and not args.mechanics_only:
        parser.exit(1, "release drill completed but is not qualification-eligible\n")


if __name__ == "__main__":
    main()
