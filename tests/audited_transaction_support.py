"""Trusted fixture bootstrap and transport; no policy, signing, or host facts."""
import hashlib

import pytest

from conftest import PI_ROOT, SIGIL_ROOT
from readiness_host_support import ROOT, readiness_host_binary as readiness_host_binary
from scripts.compose_application import compose_application
from scripts.compose_transaction_audit import compose_transaction_audit
from store_support import LIMITS, NativeStore
from turn_support import fields
from worker_support import NativeWorker

CHAIN = "b" * 64
HEADS, ENTRIES = "audit.heads", "audit.entries"
KEY_ENV = "PI_AUDITED_TRANSACTION_FIXTURE_KEY"
KEY = "native-only-fixture-key-not-a-product-credential-0123456789"


@pytest.fixture(scope="session")
def audited_native(readiness_host_binary):
    assert readiness_host_binary.is_file()
    return (readiness_host_binary.with_name("sigil-transaction"),
            ROOT / "native/store/target/debug/sigil-store")


@pytest.fixture
def retained_audit(audited_native, terminal_records, tmp_path):
    row, records, writes = terminal_records
    root = tmp_path / "records"
    with NativeStore(audited_native[1], root, row["grants"], initialize=True) as store:
        store.commit(writes)
        store.commit([{"namespace": "a.state", "key": "same-session", "revision": 1,
                       "value": fields(records[1], "SR1\n", 3)[2]}])
    return root, row


class NativeAudit(NativeWorker):
    def __init__(self, binary, root, row, *, patch=None, formatter=None, producer=None):
        def config(worker, directory):
            def value(index):
                return {"kind": "value", "index": index}

            def literal(text):
                return {"kind": "literal", "value": text}

            def read(namespace, key):
                return {"kind": "read", "namespace": namespace, "key": key}

            source = formatter or compose_transaction_audit(PI_ROOT, SIGIL_ROOT).text
            path = directory / "audit.sigil"
            path.write_text(source)
            self.formatter_path = path
            self.formatter_source = source
            audit_worker = {**worker, "source": str(path), "source_sha256": hashlib.sha256(source.encode()).hexdigest()}
            result = {"version": 2, "state_root": str(root), "limits": LIMITS,
                "worker": worker, "marker": "SF1\n", "values": 2,
                "grants": {"a.operations": "read_write", "a.reservation": "read_write", "a.budget": "read_write", "a.state": "read"},
                "inputs": [literal(row["facts"]), literal("b" * 64), value(0), value(1), {"kind": "clock"},
                    read("a.operations", value(0)), read("a.state", value(1)), read("a.reservation", value(0)),
                    read("a.budget", literal("active"))],
                "audit": {"version": 1, "worker": audit_worker, "key_env": KEY_ENV,
                    "chain": CHAIN, "heads": HEADS, "records": ENTRIES,
                    "limits": {"payload_bytes": 16384, "records": 100, "bytes": 1_000_000}}}
            if patch:
                patch(result)
            self.config = result
            return result
        super().__init__(binary, producer or compose_application("settlement", SIGIL_ROOT).text, storage=config)
        assert self.ready["protocol"] == "sigil-transaction/v2"

    def apply(self):
        return self.request({"op": "apply", "values": ["a" * 64, "same-session"]})


def inspect(binary, root, row, *, count=1):
    grants = {**row["grants"], HEADS: "read", ENTRIES: "read"}
    with NativeStore(binary, root, grants) as store:
        records = [store.get(ns, key) for ns, key in [("a.operations", "a" * 64),
            ("a.reservation", "a" * 64), ("a.budget", "active"), ("a.state", "same-session")]]
        head = store.get(HEADS, CHAIN)
        entries = [store.get(ENTRIES, f"{CHAIN}.{i:016x}") for i in range(count)]
        return records, head, entries
