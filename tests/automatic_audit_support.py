"""Static audited HTTP deployment; SIGIL/native service drives all work."""
import copy
from contextlib import closing
import hashlib
import hmac
import json
import select
import sqlite3
import subprocess

from conftest import PI_ROOT, SIGIL_ROOT
from http_service_support import HttpApi
from readiness_admission_host_support import configure_admission
from readiness_admission_support import source as entry_source
from scripts.compose_transaction_audit import compose_transaction_audit
from scripts.compose_effect_audit import compose_effect_audit


def audit_key(index):
    return f"native-only-automatic-audit-fixture-key-{index}-0123456789"


def effect_audit_key(index):
    return f"native-only-effect-audit-fixture-key-{index}-0123456789"


def configure(root, programs, endpoint, workspace, *, rows=None, patch=None, diagnostic=False, audit_effects=False):
    config, path = configure_admission(root, programs, endpoint, workspace, limit=1024, rows=rows)
    # Existing helpers share the baseline LIMITS dictionary. Negative deployment
    # mutations must never alter that original fixture or later test profiles.
    config = copy.deepcopy(config)
    if not diagnostic:
        # The real draft SIGIL entry, without the test-only final readiness reply.
        source = entry_source(1024)
        entry = root / "audited-http-entry.sigil"
        entry.write_text(source.text)
        config["worker"].update(source=str(entry), source_sha256=source.compiler_input_sha256)
    source = compose_transaction_audit(PI_ROOT, SIGIL_ROOT)
    audit = root / "transaction-audit.sigil"
    audit.write_text(source.text)
    if audit_effects:
        effect_source = compose_effect_audit(PI_ROOT, SIGIL_ROOT)
        effect_path = root / "effect-audit.sigil"
        effect_path.write_text(effect_source.text)
    for index, row in enumerate(config["automatic"]["participants"]):
        row["transaction_audit"] = {
            "version": 1,
            "worker": {**config["worker"], "source": str(audit), "source_sha256": source.compiler_input_sha256},
            "key_env": f"PI_AUTOMATIC_AUDIT_FIXTURE_KEY_{index}",
            "chain": hashlib.sha256(f"fixture-chain-{index}".encode()).hexdigest(),
            "heads": f"audit{index}.heads", "records": f"audit{index}.records",
            "limits": {"payload_bytes": 16384, "records": 100, "bytes": 1_000_000},
        }
        if audit_effects:
            row["effect_audit"] = {
                "version": 1,
                "worker": {**config["worker"], "source": str(effect_path), "source_sha256": effect_source.compiler_input_sha256},
                "key_env": f"PI_AUTOMATIC_EFFECT_AUDIT_FIXTURE_KEY_{index}",
                "chain": hashlib.sha256(f"effect-chain-{index}".encode()).hexdigest(),
                "heads": f"effects{index}.heads", "records": f"effects{index}.records",
                "limits": {"payload_bytes": 16384, "records": 100, "bytes": 1_000_000},
            }
    if patch:
        patch(config)
    path.write_text(json.dumps(config))
    return config, path


class AutomaticAuditApi(HttpApi):
    def __init__(self, binary, root, programs, endpoint, workspace, *, rows=None, patch=None,
                 diagnostic=False, mode="init", audit_effects=False):
        self.root = root
        self.config, path = configure(root, programs, endpoint, workspace, rows=rows,
                                      patch=patch, diagnostic=diagnostic, audit_effects=audit_effects)
        self.proc = subprocess.Popen([str(binary), mode, str(path), "0"], stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True)
        try:
            readable, _, _ = select.select([self.proc.stdout], [], [], 40)
            assert readable, "audited automatic startup timed out"
            line = self.proc.stdout.readline()
            assert line, self.proc.stderr.read()
            self.ready = json.loads(line)
            assert self.ready["status"] == "ready" and self.ready["protocol"] == "sigil-application-host/v9"
            host, port = self.ready["address"].split(":")
            assert host == "127.0.0.1"
            self.port = int(port)
        except BaseException:
            self.close()
            raise


def events(root, config, index, *, effects=False):
    audit = config["automatic"]["participants"][index]["effect_audit" if effects else "transaction_audit"]
    key_bytes = (effect_audit_key(index) if effects else audit_key(index)).encode()
    with closing(sqlite3.connect(f"file:{root / 'records/records.sqlite'}?mode=ro", uri=True)) as db:
        rows = db.execute("SELECT key,value FROM records WHERE namespace=? ORDER BY key", (audit["records"],)).fetchall()
    result, previous = [], "0" * 64
    for sequence, (key, value) in enumerate(rows):
        raw = bytes(value).decode()
        signed = json.loads(raw)
        body = json.dumps(signed["body"], ensure_ascii=False, separators=(",", ":")).encode()
        parts = [audit["heads"].encode(), audit["records"].encode(), body]
        framed = b"sigil-authenticated-log/record/v1\0" + b"".join(len(p).to_bytes(8, "big") + p for p in parts)
        assert hmac.compare_digest(signed["tag"], hmac.new(key_bytes, framed, hashlib.sha256).hexdigest())
        assert key == f"{audit['chain']}.{sequence:016x}"
        assert signed["body"]["chain"] == audit["chain"] and signed["body"]["sequence"] == sequence
        assert signed["body"]["previous"] == previous
        previous = hashlib.sha256(raw.encode()).hexdigest()
        result.append(json.loads(signed["body"]["payload"]))
    return result
