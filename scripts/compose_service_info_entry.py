"""Build the v9 entry with legacy health/version behavior owned by SIGIL.

No runtime Python dispatcher, extra native import or looser admission limit.
The declared build label is an explicit source input, not proof of a release.
"""

import hashlib
import json
from pathlib import Path
import re

from scripts.compose_application import ApplicationSource, strip_line_comments
from scripts.compose_readiness_admission import compose_readiness_admission
from scripts.sigil_omit import compact_operator_layout


def compose_service_info_entry(repo, stdlib, *, request_limit, version):
    if not isinstance(version, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}", version):
        raise ValueError("explicit bounded ASCII build label required; use 0.0.0-unset before release")
    baseline = compose_readiness_admission(repo, stdlib, request_limit=request_limit)
    text = baseline.text

    def replace(old, new):
        nonlocal text
        if text.count(old) != 1:
            raise ValueError("one exact reviewed service-info entry site required: " + old)
        text = text.replace(old, new)

    # This site is AFTER validated RP4 and the actual positive DC2 receipt.
    anchor = 'if is_text(get(x,0),"GET")&&is_text(get(x,1),"/v1/ready"){return readiness_begin(c,0);}'
    replace(anchor, anchor + 'let info:i64@Internal=info_begin(x,c);if info!=0{return info;}')
    # All original authentication, correlation and request accounting run first.
    anchor = 'let h:i64@Internal=alloc(40);'
    replace(anchor, 'if !booting&&info_route(x){if info_project(action,selected)<0{return-400;}}' + anchor)
    # The existing buffer holds two headers. Challenge and Retry-After cannot
    # coexist because their validated statuses are mutually exclusive.
    anchor = 'let response:i64@Internal=alloc(24);'
    replace(anchor, 'if !booting&&info_route(x)&&is_text(get(action,1),"401"){'
        'put(h,0,text("2"));put(h,3,text("www-authenticate"));'
        'put(h,4,text("Bearer realm=\\\"sigil-pi\\\""));count=5;}' + anchor)
    path = Path(repo) / "app/pi/service_info.sigil"
    raw = path.read_bytes()
    fragment = raw.decode("utf-8")
    marker = '"// SERVICE_VERSION_LITERAL"'
    if fragment.count(marker) != 1:
        raise ValueError("one declared-version literal required")
    fragment = fragment.replace(marker, json.dumps(version))
    text = compact_operator_layout(strip_line_comments(text + "\n" + fragment, compact_indent=True))
    if len(text.encode()) > 65536 or text.count("pub fn tool_main(") != 1:
        raise ValueError(f"service-info entry must preserve original source/entry ceilings: {len(text.encode())}")
    inputs = {**baseline.input_hashes,
        "app/pi/service_info.sigil": hashlib.sha256(raw).hexdigest(),
        "configuration/service-version": hashlib.sha256(version.encode()).hexdigest(),
        "scripts/compose_service_info_entry.py": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    return ApplicationSource(text, inputs, baseline.stdlib_hash)
