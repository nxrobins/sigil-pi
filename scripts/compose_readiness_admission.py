"""Draft v9 entry admission integration; NOT a complete readiness route.

The production fragment reaches RN1/inspect_execution only after confirmed
accounting. Final dependency/lifecycle interpretation remains unimplemented;
the unchanged strict continuation decoder refuses RN1 instead of inventing a
health response. No deployment recipe selects this unfinished entry.
"""

import hashlib
import json
from pathlib import Path

from scripts.compose_application import ApplicationSource, strip_line_comments
from scripts.compose_bootstrap_admission import MOVED, replace_entry_bootstrap
from scripts.compose_request_entry import compose_request_entry
from scripts.readiness_entry_base import APPROVED
from scripts.sigil_layout import compact_layout
from scripts.sigil_omit import compact_operator_layout, omit_unreferenced_functions


ROOT = Path(__file__).resolve().parent.parent
COMMANDS = '["call","commit","commit_observed","inspect_execution","inspect_storage","metadata","read","read_many","read_observed","reply","temporary_read","temporary_write"]'


def compose_readiness_admission(repo, stdlib, *, request_limit):
    baseline = compose_request_entry(repo, stdlib, request_limit=request_limit)
    inputs = dict(baseline.input_hashes)

    def compact(text):
        return compact_layout(strip_line_comments(text, compact_indent=True))

    entry = (Path(repo) / "app/pi/request_entry.sigil").read_text()
    wrapper = compact(entry.replace('"// REQUEST_LIMIT_LITERAL"', json.dumps(str(request_limit))))
    if not baseline.text.endswith(wrapper):
        raise ValueError("require the exact complete original v8 entry suffix")
    body = baseline.text[:-len(wrapper)]
    body = replace_entry_bootstrap(body, inputs)

    def replace(old, new, count=1):
        nonlocal entry
        if entry.count(old) != count:
            raise ValueError(f"expected {count} exact admission integration sites: {old[:100]}")
        entry = entry.replace(old, new)

    replace('command("read", get(c, 12), text("request-window")',
            'command("read_observed", get(c, 12), text("request-window")')
    replace('if is_text(get(x, 6), "read") && is_text(get(x, 8), "request-rate") {',
            '''if is_text(get(x, 6), "read_observed") && is_text(get(x, 8), "request-rate") {
        let seen: i64 @Internal = read_record(get(x, 7), "DR2\\n", 6);
        if seen < 0 { return seen; }
        if is_text(get(seen, 0), "error") { return readiness_emergency_begin(x, c, get(x, 7)); }
        let observed: i64 @Internal = readiness_durable_read(get(x, 7));
        if observed < 0 { return observed; }''')
    replace('put(args, 4, get(x, 7));', 'put(args, 4, observed);')
    replace('command("commit", get(rate, 1)', 'command("commit_observed", get(rate, 1)')
    replace('if is_text(get(x, 6), "commit") && starts(get(x, 8), "RP4\\n") {',
            'if is_text(get(x, 6), "commit_observed") && starts(get(x, 8), "RP4\\n") {')
    replace('''let receipt: i64 @Internal = read_record(get(x, 7), "SC1\\n", 2);
        if receipt < 0 { return receipt; }
        if is_text(get(receipt, 0), "error") && is_text(get(receipt, 1), "0") {
            return request_failure(c, 503, "storage_unavailable", text(""));
        }
        if !is_text(get(receipt, 0), "ok") || store_revision(get(receipt, 1)) < 1 { return -400; }''',
            '''let receipt: i64 @Internal = read_record(get(x, 7), "DC2\\n", 4);
        if receipt < 0 { return receipt; }
        if is_text(get(receipt, 0), "error") { return readiness_emergency_begin(x, c, get(x, 7)); }
        if !is_text(get(receipt, 0), "ok") || store_revision(get(receipt, 1)) < 1
           || size(get(receipt, 2)) != 0 || size(get(receipt, 3)) != 0 { return -400; }
        if is_text(get(x, 0), "GET") && is_text(get(x, 1), "/v1/ready") { return readiness_begin(c, 0); }''')
    replace('''    let held: i64 @Internal = read_record(get(x, 8), "RP5\\n", 2);''',
            '''    if is_text(get(x, 6), "temporary_read") || is_text(get(x, 6), "temporary_write")
       || (is_text(get(x, 6), "call") && is_text(get(x, 8), "emergency-policy")) {
        return readiness_emergency_step(x, c, limit);
    }
    let held: i64 @Internal = read_record(get(x, 8), "RP5\\n", 2);''')
    # Ordinary RP2 still accepts at most 60; only the emergency result can add 61.
    replace('number(get(result, 1), 60)', 'number(get(result, 1), 61)')
    replace('''    if is_text(get(x, 6), "init") {''',
            '''    if process_pair(get(x, 17)) < 0 { return -400; }
    if is_text(get(x, 6), "init") {''')
    replace('''if size(get(x, 13)) != 0 || size(get(x, 15)) != 0''',
            '''if size(get(x, 13)) != 0 || size(get(x, 15)) != 0 || size(get(x, 17)) != 0''')
    replace('"// REQUEST_LIMIT_LITERAL"', json.dumps(str(request_limit)))

    # All new-operation paths reach this existing SIGIL function only AFTER
    # deduplication lookup; retries of accepted operations never pass this gate.
    anchor = compact('''fn grouped_admit(x: i64 @Internal, c: i64 @Internal, key: i64 @Internal,
                 held: i64 @Internal, prior: i64 @Internal) -> i64 @Internal ! { Alloc } {''')
    if body.count(anchor) != 1:
        raise ValueError("require the exact new-operation admission boundary")
    body = body.replace(anchor, anchor + compact('''
    let lifecycle: i64 @Internal = lifecycle_turn_gate(x);
    if lifecycle != 0 { return lifecycle; }
'''))

    fragments = ""
    for name in ("emergency_guards.sigil", "readiness_admission.sigil", "lifecycle.sigil"):
        path = ROOT / "app/pi" / name
        raw = path.read_bytes()
        inputs["app/pi/" + name] = hashlib.sha256(raw).hexdigest()
        fragments += raw.decode() + "\n"
    joined = fragments + entry
    # Share the identical guarded-command wrapper. This changes no command,
    # time check, error propagation or continuation, and adds no execution step.
    prefix = 'request_action(time_guard(command('
    suffix = '), get(c, 3), get(c, 4)), text(""))'
    if joined.count(prefix) != 7 or joined.count(suffix) != 7:
        raise ValueError("require seven exact guarded-command wrapper sites")
    joined = joined.replace(prefix, 'request_guard(c, command(').replace(suffix, '))')
    text = body + compact(joined)
    # Compact using the ORIGINAL reviewed rule before applying exact helper
    # fingerprints. Final operator layout preserves the full remaining program.
    def replace_wire(old, new, count):
        nonlocal text
        if text.count(old) != count:
            raise ValueError(f"expected {count} exact v9 profile sites: {old}")
        text = text.replace(old, new)

    replace_wire('"AH6\\n",17', '"AH7\\n",18', 4)
    replace_wire('"HC6\\n"', '"HC7\\n"', 12)
    replace_wire('arr_len(get(x,10))!= 4', 'arr_len(get(x,10))!= 5', 1)
    replace_wire('!is_text(item(get(x,10),1),"history")|| !is_text(item(get(x,10),2),"listing")|| !is_text(item(get(x,10),3),"request_policy")',
                 '!is_text(item(get(x,10),1),"emergency_policy")|| !is_text(item(get(x,10),2),"history")|| !is_text(item(get(x,10),3),"listing")|| !is_text(item(get(x,10),4),"request_policy")', 1)
    replace_wire(json.dumps('["call","commit","metadata","read","read_many","reply"]'),
                 json.dumps(COMMANDS), 1)
    text = compact_operator_layout(omit_unreferenced_functions(text, {**APPROVED, **MOVED}).text)
    if len(text.encode()) > 65536 or text.count("pub fn tool_main(") != 1:
        raise ValueError(f"draft admission entry exceeds original source/entry ceilings: {len(text.encode())}")
    for name in ("compose_readiness_admission.py", "readiness_entry_base.py", "sigil_omit.py"):
        inputs["scripts/" + name] = hashlib.sha256((ROOT / "scripts" / name).read_bytes()).hexdigest()
    return ApplicationSource(text, inputs, baseline.stdlib_hash)
