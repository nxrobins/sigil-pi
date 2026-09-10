"""Build-only composition of the fixed SIGIL evaluation-failure classifier."""
import hashlib
from pathlib import Path

from scripts.compose_application import ApplicationSource, strip_line_comments
from scripts.compose_transaction_audit import compose_transaction_audit

ROOT = Path(__file__).resolve().parent.parent


def compose_evaluation_audit(repo, stdlib):
    base = compose_transaction_audit(repo, stdlib)
    marker = "pub fn tool_main("
    if base.text.count(marker) != 1:
        raise ValueError("require exactly one original audit entry")
    prefix = base.text.split(marker)[0]
    if prefix.count("module pi_transaction_audit;") != 1:
        raise ValueError("require the original audit module declaration")
    prefix = prefix.replace("module pi_transaction_audit;", "module pi_evaluation_audit;")
    source = ROOT / "app/pi/evaluation_audit.sigil"
    raw = source.read_bytes()
    text = prefix + strip_line_comments(raw.decode(), compact_indent=True)
    if len(text.encode()) > 65536 or text.count(marker) != 1:
        raise ValueError("preserve original compiler source and entry limits")
    inputs = {**base.input_hashes,
              "app/pi/evaluation_audit.sigil": hashlib.sha256(raw).hexdigest(),
              "scripts/compose_evaluation_audit.py": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    return ApplicationSource(text, inputs, base.stdlib_hash)
