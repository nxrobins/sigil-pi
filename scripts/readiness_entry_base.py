"""Prepare a smaller v8 base for the proposed readiness entry, not a new API.

The enabled v8 recipe is unchanged. This draft may omit only five exact helper
definitions that are unused by its fixed tool_main execution path. The complete
remaining source still needs fresh compiler admission and regression tests.
"""

from dataclasses import dataclass
import hashlib
from pathlib import Path

from scripts.compose_application import ApplicationSource
from scripts.compose_request_entry import compose_request_entry
from scripts.sigil_omit import Omission, compact_operator_layout, omit_unreferenced_functions


APPROVED = {
    "parse_field": "536312a1bbd9b7fd2dda0ce74e2eefaf7d9d8a749d65758b4209d392aa4b4524",
    "key_matches": "02e3b071aac20b43cb61d09abc1fdd98197cdf9594b09dff2b332bad1147c1bc",
    "bytes_eq": "05bd2730254656e3a6c3a38902e69647f798b85feb3ebe6d6e07a1f9773dd8ed",
    "store_quoted_size": "151303ea4ec018ea7a2b2b8ec281682b7df6731ff8293f26db84171f01f182fe",
    "action_key": "d32e6c8e79a97980756ea8239189aa5c1d39e17a4c1d0b21e2d4b700b6079282",
}


@dataclass(frozen=True)
class EntryBase(ApplicationSource):
    original: str
    omissions: tuple[Omission, ...]
    layout_only: str


def prepare_entry_base(repo, stdlib, *, request_limit):
    baseline = compose_request_entry(repo, stdlib, request_limit=request_limit)
    reduced = omit_unreferenced_functions(baseline.text, APPROVED)
    compact = compact_operator_layout(reduced.text)
    if len(compact.encode()) > 65536:
        raise ValueError("entry base must preserve the original source ceiling")
    inputs = dict(baseline.input_hashes)
    for name in ("readiness_entry_base.py", "sigil_omit.py"):
        inputs["scripts/" + name] = hashlib.sha256((Path(__file__).parent / name).read_bytes()).hexdigest()
    return EntryBase(compact, inputs, baseline.stdlib_hash, baseline.text, reduced.removed,
                     compact_operator_layout(baseline.text))
