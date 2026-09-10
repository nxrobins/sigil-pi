#!/usr/bin/env python3
"""Copy an explicit native deployment's admitted bytes; never run its service.

This is build/install tooling, not API, agent, authorization or recovery policy.
The output is private, path-bound development material, NOT a qualified release.
An existing deployment/state directory is never overwritten or migrated.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import stat


HASH = re.compile(r"[0-9a-f]{64}\Z")
WORKER_FIELDS = {"version", "runtime", "runtime_sha256", "source", "source_sha256",
                 "max_fuel", "max_timeout_ms", "net", "fs", "secret_env"}
CONFIG_FIELDS = {"version", "worker", "functions", "state_root", "limits", "credentials",
                 "automatic", "http", "process_facts"}
SOURCE_BYTES = 65536
RUNTIME_BYTES = 256 * 1024 * 1024


class DeploymentError(ValueError):
    pass


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


def _absolute(value):
    if not isinstance(value, (str, Path)):
        raise DeploymentError("an explicit absolute path is required")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise DeploymentError("an explicit absolute path without parent traversal is required")
    return path


def _safe_mode(mode, executable):
    return mode & 0o6022 == 0 and (not executable or mode & 0o111 != 0)


def _read(path, limit, *, executable=False):
    path = _absolute(path)
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        with os.fdopen(os.open(path, flags), "rb") as stream:
            before = os.fstat(stream.fileno())
            if (not stat.S_ISREG(before.st_mode) or before.st_size > limit
                    or not _safe_mode(before.st_mode, executable)):
                raise DeploymentError("artifact type, mode or size is not permitted")
            raw = stream.read(limit + 1)
            after = os.fstat(stream.fileno())
        if (not raw or len(raw) > limit or len(raw) != before.st_size
                or (before.st_size, before.st_mtime_ns, before.st_ctime_ns, before.st_mode)
                != (after.st_size, after.st_mtime_ns, after.st_ctime_ns, after.st_mode)):
            raise DeploymentError("artifact changed or exceeded its bound")
        return raw
    except OSError as error:
        raise DeploymentError("artifact could not be read as a regular file") from error


def _pairs(items):
    result = {}
    for name, value in items:
        if name in result:
            raise DeploymentError("duplicate configuration member")
        result[name] = value
    return result


def _constant(_):
    raise DeploymentError("non-finite configuration number")


def _json(raw):
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise DeploymentError("invalid UTF-8 JSON configuration") from error


def _encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                       allow_nan=False) + "\n").encode()


def _physical_destination(path):
    try:
        parent = path.parent.resolve(strict=True)
        if not parent.is_dir():
            raise DeploymentError("destination parent must be an existing directory")
    except (OSError, RuntimeError) as error:
        raise DeploymentError("destination parent must be an existing directory") from error
    return parent / path.name


def _disjoint(first, second):
    return first != second and first not in second.parents and second not in first.parents


def worker_sites(config):
    """Enumerate actual v9 worker slots, never search arbitrary caller payloads."""
    if (not isinstance(config, dict) or set(config) - CONFIG_FIELDS
            or type(config.get("version")) is not int or config["version"] != 9):
        raise DeploymentError("an explicit native v9 configuration is required")
    try:
        result = [("entry", config["worker"])]
        result += [("function/" + key, value) for key, value in config["functions"].items()]
        for index, participant in enumerate(config["automatic"]["participants"]):
            base = "participant/" + str(index)
            result.append((base, participant["worker"]))
            for alias, effect in participant["effects"].items():
                prefix = base + "/effect/" + alias
                result += [(prefix, effect["worker"]), (prefix + "/policy", effect["policy"]["worker"]),
                           (prefix + "/recorder", effect["recorder"]["worker"])]
            result += [(base + "/transaction/" + alias, value["worker"])
                       for alias, value in participant["transactions"].items()]
            for kind in ("transaction_audit", "effect_audit"):
                if kind in participant:
                    audit = participant[kind]
                    result.append((base + "/" + kind, audit["worker"]))
                    if "evaluation" in audit:
                        result.append((base + "/" + kind + "/evaluation", audit["evaluation"]))
        if not config["automatic"]["participants"]:
            raise DeploymentError("an automatic application configuration is required")
    except (KeyError, TypeError, AttributeError) as error:
        raise DeploymentError("incomplete native worker inventory") from error
    if any(not isinstance(worker, dict) or set(worker) != WORKER_FIELDS for _, worker in result):
        raise DeploymentError("a worker descriptor is incomplete or has unknown fields")
    return result


def stage_deployment(*, config_path, host_path, host_sha256, assets_path, output, state_root):
    """Materialize byte-exact artifacts and an explicitly relocated configuration.

    No environment secret is read; network, filesystem, secret, policy, credential,
    deadline and allowance fields are unchanged. The native/SIGIL startup checks
    remain authoritative. This operation does not start them or prove admission.
    """
    output, state_root = _absolute(output), _absolute(state_root)
    if not _disjoint(output, state_root):
        raise DeploymentError("artifact and state directories must be disjoint")
    if os.path.lexists(output) or os.path.lexists(state_root):
        raise DeploymentError("destination or state already exists; no overwrite or migration")
    if not _disjoint(_physical_destination(output), _physical_destination(state_root)):
        raise DeploymentError("artifact and state directories must be physically disjoint")
    original = _read(config_path, 4 * 1024 * 1024)
    config = copy.deepcopy(_json(original))
    sites = worker_sites(config)
    assets = _json(_read(assets_path, 16384))
    if (not isinstance(assets, dict) or set(assets) != {"version", "assets"}
            or type(assets["version"]) is not int or assets["version"] != 1
            or not isinstance(assets["assets"], list) or not 1 <= len(assets["assets"]) <= 8):
        raise DeploymentError("an explicit bounded browser asset manifest is required")
    for asset in assets["assets"]:
        if not isinstance(asset, dict) or set(asset) != {"route", "path", "sha256", "content_type"}:
            raise DeploymentError("invalid browser asset descriptor")
    # Exclusive, private destination. Failure leaves no completion manifest and
    # never removes or modifies source artifacts, existing state or deployments.
    output.mkdir(mode=0o700)
    # Also detect filesystem name equivalence (case/normalization) only visible
    # after exclusive creation. No artifact/configuration is written on refusal.
    if os.path.lexists(state_root):
        raise DeploymentError("artifact and state directories must remain disjoint")
    rows, written, role_inventory = [], {}, []

    def write(relative, raw, mode=0o600):
        target = output / relative
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with target.open("xb") as stream:
            stream.write(raw)
        target.chmod(mode)
        rows.append({"path": relative, "sha256": _hash(raw), "bytes": len(raw), "mode": mode})
        return str(target)

    def artifact(path, expected, kind):
        if not isinstance(expected, str) or not HASH.fullmatch(expected):
            raise DeploymentError("invalid artifact digest")
        raw = _read(path, RUNTIME_BYTES if kind in {"runtime", "host"} else SOURCE_BYTES,
                    executable=kind in {"runtime", "host"})
        if _hash(raw) != expected:
            raise DeploymentError("artifact digest does not match the supplied identity")
        relative = "bin/sigil-application-host" if kind == "host" else kind + "/" + expected
        if relative in written:
            return written[relative]
        target = write(relative, raw, 0o700 if kind in {"runtime", "host"} else 0o600)
        written[relative] = target
        return target

    executable = artifact(host_path, host_sha256, "host")
    for role, worker in sites:
        worker["runtime"] = artifact(worker["runtime"], worker["runtime_sha256"], "runtime")
        worker["source"] = artifact(worker["source"], worker["source_sha256"], "source")
        role_inventory.append({"role": role, "source_sha256": worker["source_sha256"],
                               "runtime_sha256": worker["runtime_sha256"]})
    for asset in assets["assets"]:
        asset["path"] = artifact(asset["path"], asset["sha256"], "public")
    config["state_root"] = str(state_root)
    config_file = write("service.json", _encoded(config))
    assets_file = write("public-assets.json", _encoded(assets))
    launch = [executable, "init", config_file, "0", "--public-assets", assets_file]
    opened = [executable, "open", config_file, "0", "--public-assets", assets_file]
    evidence = {"schema": "sigil-pi/native-deployment/v1", "status": "development-unqualified",
                "input_configuration_sha256": _hash(original), "workers": role_inventory,
                "files": sorted(rows, key=lambda row: row["path"]), "init": launch, "open": opened}
    # Written last: a complete inventory, not a certificate or runtime receipt.
    write("DEPLOYMENT.json", _encoded(evidence))
    return evidence


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("config", "host", "host-sha256", "assets", "output", "state-root"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    result = stage_deployment(config_path=args.config, host_path=args.host,
        host_sha256=args.host_sha256, assets_path=args.assets, output=args.output, state_root=args.state_root)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
