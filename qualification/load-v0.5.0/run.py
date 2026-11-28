"""Run the release load harness on an isolated Linux host with Inkling on the Docker Desktop host."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
from datetime import datetime, timezone
import secrets
import subprocess
import sys
import tempfile
import threading
import time
from inkling import Provider, MODEL, identity

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.build_release import verify_release
from scripts.failure_drill import _Service
from scripts.load_test import _parser, validate_run, load_credentials
from scripts.release_drill import _install_artifact, _create_private

ARCHIVE = "136dc95ac105f76ffb46a67b27a83ef8029a965140e303c6dd8a5d97f8dbb5eb"


def credentials(folder):
    now = int(time.time())
    policies, tenants = [], []
    for index in range(26):
        ops = index == 25
        tenant = "benchmark-ops" if ops else f"load-{index:02d}"
        token = secrets.token_urlsafe(32)
        policies.append(
            {
                "sha256": hashlib.sha256(token.encode()).hexdigest(),
                "tenant": tenant,
                "principal": tenant,
                "scopes": ["ops:read"] if ops else ["chat"],
                "tools": [] if ops else ["list_dir"],
                "not_before_unix": now - 60,
                "expires_unix": now + 4 * 3600,
            }
        )
        if ops:
            ops_token = token
        else:
            tenants.append({"tenant": tenant, "chat_token": token})
    auth, clients = folder / "service-auth.json", folder / "client-auth.json"
    _create_private(auth, json.dumps({"tokens": policies}))
    _create_private(clients, json.dumps({"ops_token": ops_token, "tenants": tenants}))
    return ops_token, auth, clients


def process_resources(pid):
    directory = Path("/proc") / str(pid)
    status = (directory / "status").read_text()
    return {
        "pid": pid,
        "rss_bytes": int(re.search(r"^VmRSS:\s+(\d+) kB$", status, re.M)[1]) * 1024,
        "threads": int(re.search(r"^Threads:\s+(\d+)$", status, re.M)[1]),
        "fds": len(list((directory / "fd").iterdir())),
    }


def sample_processes(service, stop, samples):
    while True:
        sample = {"time_unix": time.time()}
        try:
            pids = sorted(service.runtime_pids())
            host = process_resources(service.process.pid)
            children = [process_resources(pid) for pid in pids]
            if not pids or pids != sorted(service.runtime_pids()):
                raise ValueError("compiler set changed during sample")
            sample.update(host=host, compilers=children)
        except (OSError, IndexError, TypeError, ValueError) as error:
            sample["error"] = type(error).__name__
        samples.append(sample)
        if stop.wait(5):
            return


def measure(archive, output, *, preflight=False):
    before = identity()
    harness = ROOT / "scripts/load_test.py"
    fixture_files = [
        Path(__file__),
        Path(__file__).with_name("inkling.py"),
        Path(__file__).with_name("verify.py"),
        harness,
        ROOT / "scripts/failure_drill.py",
        ROOT / "scripts/release_drill.py",
        ROOT / "scripts/build_release.py",
    ]
    sources = {
        str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in fixture_files
    }
    if sources["scripts/load_test.py"] != "bfbc5adc11c2744350079c0115e5be21bad2aa396613d77e01b6d0037d54f0a9":
        raise RuntimeError("load harness differs from v0.5.0")
    record = json.loads((ROOT / "docs/evidence/candidate.json").read_text())
    if (record["sha256"], record["version"], record["tag"]) != (ARCHIVE, "0.5.0", "v0.5.0"):
        raise RuntimeError("candidate record differs from published v0.5.0")
    verify_release(archive, record=record)
    if platform.machine() not in ("x86_64", "amd64") or sys.platform != "linux":
        raise RuntimeError("the published archive requires Linux x86_64")
    os.umask(0o077)
    output.mkdir(parents=True, exist_ok=False)
    with tempfile.TemporaryDirectory(prefix="v050-load-") as temporary, Provider() as provider:
        work = Path(temporary)
        installed = _install_artifact(archive, work / "release", "candidate")
        if (installed["sha256"], installed["version"]) != (record["sha256"], record["version"]):
            raise RuntimeError("installed release identity differs from candidate")
        ops, auth, clients = credentials(work)
        state = work / "state"
        state.mkdir()
        thread = threading.Thread(target=provider.serve_forever)
        thread.start()
        service = _Service(
            installed["root"],
            state,
            auth_file=auth,
            token=ops,
            audit_key=secrets.token_hex(32),
            label="load",
            settings={"PI_MODEL": MODEL},
            endpoint=f"http://127.0.0.1:{provider.server_port}/v1/messages",
        )
        report_file = output / "load-test.json"
        stop, rss = threading.Event(), []
        sampler = None
        started = datetime.now(timezone.utc).isoformat()
        clock_start = time.monotonic()
        run = None
        cleanup_error = None
        run_error = None
        try:
            service.start()
            sampler = threading.Thread(target=sample_processes, args=(service, stop, rss))
            sampler.start()
            cmd = [
                sys.executable,
                str(ROOT / "scripts/load_test.py"),
                "--target",
                f"http://127.0.0.1:{service.port}",
                "--credentials",
                str(clients),
                "--artifact-digest",
                record["sha256"],
                "--version",
                record["version"],
                "--concurrency",
                "2" if preflight else "50",
                "--duration",
                "75" if preflight else "3600",
                "--output",
                str(report_file),
                "--smoke-test",
            ]
            parsed = _parser().parse_args(cmd[2:])
            _, tenant_tokens = load_credentials(clients)
            _, notes = validate_run(parsed, len(tenant_tokens))
            if len(tenant_tokens) != 25 or (
                not preflight and notes != ["forecast approval identity is missing"]
            ):
                raise RuntimeError("unexpected workload prerequisites")
            started = datetime.now(timezone.utc).isoformat()
            clock_start = time.monotonic()
            run = subprocess.run(cmd, check=False)
        except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as error:
            run_error = type(error).__name__
        finally:
            elapsed = time.monotonic() - clock_start
            ended = datetime.now(timezone.utc).isoformat()
            stop.set()
            if sampler is not None:
                sampler.join()
            try:
                service.drain()
            except (RuntimeError, OSError, subprocess.SubprocessError) as error:
                cleanup_error = type(error).__name__
            finally:
                provider.shutdown()
                thread.join()
                provider.server_close()
        context = {
            "release_tag": "v0.5.0",
            "artifact_sha256": record["sha256"],
            "report_sha256": hashlib.sha256(report_file.read_bytes()).hexdigest()
            if report_file.exists()
            else None,
            "source_sha256": sources,
            "started_at": started,
            "ended_at": ended,
            "harness_elapsed_monotonic_seconds": elapsed,
            "platform": platform.platform(),
            "architecture": platform.machine(),
            "python": platform.python_version(),
            "cpus": len(os.sched_getaffinity(0)),
            "provider_before": before,
            "provider_counts": dict(provider.counts),
            "product_workers": 1,
            "service_setting_overrides": {"PI_MODEL": MODEL},
            "rss_sample_period_seconds": 5,
            "process_samples": rss,
            "harness_exit": run.returncode if run else None,
            "run_error": run_error,
            "cleanup_error": cleanup_error,
            "preflight": preflight,
        }
        try:
            context["provider_after"] = identity()
        except (OSError, ValueError, RuntimeError, StopIteration) as error:
            context["provider_after_error"] = type(error).__name__
        (output / "run-context.json").write_text(json.dumps(context, indent=2, sort_keys=True) + "\n")
        if not report_file.exists():
            raise RuntimeError("harness produced no raw report; see run context")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("archive", type=Path)
    p.add_argument("output", type=Path)
    p.add_argument("--preflight", action="store_true")
    args = p.parse_args()
    measure(args.archive, args.output.resolve(), preflight=args.preflight)
