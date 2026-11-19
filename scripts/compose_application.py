"""Build-time composition for fixed SIGIL application components, not runtime policy.

Original input hashes and the final compiler-input hash stay available separately.
Line comments are removed lexically to keep the composed application within the
pinned forge's existing 64 KiB source limit; strings/block comments are preserved.
No compiler limit, certificate requirement or product gate is changed.
"""

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

from sigil_compose import compose_with_stdlib


ROOT = Path(__file__).resolve().parent.parent


def strip_line_comments(source, *, compact_indent=False):
    out, i, quote, block_depth = [], 0, None, 0
    while i < len(source):
        ch = source[i]
        if block_depth:
            if source.startswith("/*", i):
                block_depth += 1
                out.append("/*")
                i += 2
            elif source.startswith("*/", i):
                block_depth -= 1
                out.append("*/")
                i += 2
            else:
                out.append(ch)
                i += 1
        elif quote:
            out.append(ch)
            i += 1
            if ch == "\\" and i < len(source):
                out.append(source[i])
                i += 1
            elif ch == quote:
                quote = None
        elif source.startswith("/*", i):
            block_depth = 1
            out.append("/*")
            i += 2
        elif source.startswith("//", i):
            end = source.find("\n", i)
            i = len(source) if end < 0 else end
            out.append(" ")
        elif compact_indent and ch in " \t" and (i == 0 or source[i - 1] == "\n"):
            # Layout only, outside literals/block comments. Preserve every newline,
            # token and literal byte; original source hashes remain in the manifest.
            while i < len(source) and source[i] in " \t":
                i += 1
        else:
            out.append(ch)
            if ch in "\"'":
                quote = ch
            i += 1
    return "".join(out)


@dataclass(frozen=True)
class ApplicationSource:
    text: str
    input_hashes: dict[str, str]
    stdlib_hash: str

    @property
    def compiler_input_sha256(self):
        return hashlib.sha256(self.text.encode()).hexdigest()


def compose_application(name, stdlib_root, *, root=ROOT):
    if name not in {"submission", "delivery_state", "turn", "read_request", "turn_transaction", "turn_completion", "executor_transaction", "worker_completion", "api", "api_discovery", "admission", "history", "listing", "dispatch", "settlement", "coordinator", "preclaim", "dispatch_cancellable", "preclaim_cancellable"}:
        raise ValueError(f"unknown application component: {name}")
    inputs = {}

    def read(relative):
        raw = (Path(root) / relative).read_bytes()
        inputs[relative] = hashlib.sha256(raw).hexdigest()
        return raw.decode("utf-8")

    def splice(body, marker, fragment):
        if body.count(marker) != 1:
            raise ValueError(f"expected exactly one composition marker: {marker}")
        return body.replace(marker, fragment)

    def region(body, name):
        begin, end = f"// {name}_BEGIN", f"// {name}_END"
        if body.count(begin) != 1 or body.count(end) != 1 or body.index(begin) >= body.index(end):
            raise ValueError(f"expected exactly one ordered region: {name}")
        return body.split(begin)[1].split(end)[0]

    if name == "api_discovery":
        # Explicit v5/v6 build profile over the SAME SIGIL API core. Retain the
        # original v3/v4 compiler input for compatibility and existing work.
        # These are build-time ABI substitutions, never Python request policy.
        base = compose_application("api", stdlib_root, root=root)
        if base.compiler_input_sha256 != "95a7dbc7986cb1e51fc6613eef7e972096553807d7368ab1f72aff57813ec373":
            raise ValueError("discovery API baseline changed; review/version before rebasing")
        inputs.update(base.input_hashes)
        body = base.text

        def replace(old, new, count=1):
            nonlocal body
            if body.count(old) != count:
                raise ValueError(f"expected {count} ABI composition sites: {old}")
            body = body.replace(old, new)

        replace('"AH3\\n", 12', '"AH4\\n", 13', 2)
        replace('"HC3\\n"', '"HC4\\n"', 8)
        replace('is_text(path, "/v1/sessions")', 'is_text(history_path(path), "/v1/sessions")')
        replace('arr_len(get(x, 10)) != 2', 'arr_len(get(x, 10)) != 3')
        commands = '["call","commit","metadata","read","reply"]'
        replace('!is_text(item(get(x, 10), 1), "history")',
                '!is_text(item(get(x, 10), 1), "history") || !is_text(item(get(x, 10), 2), "listing")'
                + ' || !is_text(get(x, 12), ' + json.dumps(commands) + ')')
        replace('fn api_decision(', read("app/pi/listing_api.sigil") + '\nfn api_decision(')
        replace('let submitting: bool @Internal =',
                'if is_text(get(x, 0), "GET") && is_text(history_path(get(x, 1)), "/v1/sessions") { return listing_api(x, c); }\n'
                'let submitting: bool @Internal =')
        # The versioned recipe itself is an authored input, independent of an
        # alternate source root used by conformance tests.
        inputs["scripts/compose_application.py"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        return ApplicationSource(strip_line_comments(body, compact_indent=True), inputs, base.stdlib_hash)
    if name == "listing":
        body = read("app/pi/listing.sigil")
        body = splice(body, "// TURN_HELPERS", read("app/pi/turn_helpers.sigil"))
        body = splice(body, "// RECORD_HELPERS", read("app/shared/record_helpers.sigil"))
        body = splice(body, "// UTF8_VALIDATOR", read("app/shared/utf8.sigil"))
        body = splice(body, "// STORE_READ_HELPERS", region(read("app/shared/store_protocol.sigil"), "STORE_READ_HELPERS"))
        modules = ["json"]
    elif name == "history":
        body = read("app/pi/history.sigil")
        body = splice(body, "// STATE_CODEC", region(read("app/pi/turn.sigil"), "STATE_CODEC"))
        body = splice(body, "// STORE_READ_HELPERS", region(read("app/shared/store_protocol.sigil"), "STORE_READ_HELPERS"))
        body = splice(body, "// OPERATION_IDENTITY", region(read("app/pi/api.sigil"), "OPERATION_IDENTITY"))
        body = splice(body, "// TURN_HELPERS", read("app/pi/turn_helpers.sigil"))
        body = splice(body, "// RECORD_HELPERS", read("app/shared/record_helpers.sigil"))
        body = splice(body, "// UTF8_VALIDATOR", read("app/shared/utf8.sigil"))
        modules = ["json"]
    elif name == "coordinator":
        body = read("app/pi/coordinator.sigil")
        body = splice(body, "// CANCEL_RECORDS", read("app/pi/cancel_records.sigil"))
        api = read("app/pi/api.sigil")
        decoder = read("app/pi/submission.sigil")
        body = splice(body, "// AUTHORITY_HELPERS", region(api, "AUTHORITY_FACTS"))
        body = splice(body, "// AUTHORITY_LEXERS", "".join(region(decoder, label) for label in
                      ("WHITESPACE_HELPER", "STRING_END_HELPER", "IDENTIFIER_HELPER")))
        body = splice(body, "// OPERATION_IDENTITY", region(api, "OPERATION_IDENTITY"))
        body = splice(body, "// STATE_CODEC", region(read("app/pi/turn.sigil"), "STATE_CODEC"))
        body = splice(body, "// STORE_READ_HELPERS", region(read("app/shared/store_protocol.sigil"), "STORE_READ_HELPERS"))
        body = splice(body, "// TURN_HELPERS", read("app/pi/turn_helpers.sigil"))
        body = splice(body, "// RECORD_HELPERS", read("app/shared/record_helpers.sigil"))
        body = splice(body, "// UTF8_VALIDATOR", read("app/shared/utf8.sigil"))
        modules = ["json"]
    elif name in {"dispatch", "settlement", "preclaim", "dispatch_cancellable", "preclaim_cancellable"}:
        dispatch = name in {"dispatch", "dispatch_cancellable"}
        preclaim = name in {"preclaim", "preclaim_cancellable"}
        body = read(f"app/pi/{'dispatch' if dispatch else 'settlement'}.sigil")
        if name == "dispatch_cancellable":
            entry = "pub fn tool_main(input_ptr: i64, input_len: i64)"
            if body.count(entry) != 1:
                raise ValueError("expected exactly one dispatch entrypoint")
            body = body.replace(entry, "fn dispatch_base(input_ptr: i64 @Internal, input_len: i64 @Internal)")
            body += "\n" + read("app/pi/dispatch_cancellable.sigil")
        if preclaim:
            entry = "pub fn tool_main(input_ptr: i64, input_len: i64)"
            if body.count(entry) != 1:
                raise ValueError("expected exactly one settlement entrypoint")
            body = body.replace(entry, "fn settle_proposed_state(input_ptr: i64 @Internal, input_len: i64 @Internal)")
            body += "\n" + read("app/pi/preclaim.sigil")
            entry = (read("app/pi/preclaim_cancellable.sigil") if name == "preclaim_cancellable" else
                'pub fn tool_main(input_ptr: i64, input_len: i64) -> i64 @Internal ! { Alloc } {\n'
                '    if input_len > 4194304 || !valid_utf8(input_ptr, input_len) { return -400; }\n'
                '    let x: i64 @Internal = read_record(slice(input_ptr, input_len), "UF1\\n", 15);\n'
                '    if x < 0 { return x; }\n'
                '    return finalize_unclaimed(x, false, text(""));\n}\n')
            body = splice(body, "// PRECLAIM_ENTRYPOINT", entry)
        if name in {"dispatch_cancellable", "preclaim_cancellable"}:
            body = splice(body, "// CANCEL_RECORDS", read("app/pi/cancel_records.sigil"))
        api = read("app/pi/api.sigil")
        decoder = read("app/pi/submission.sigil")
        turn = read("app/pi/turn.sigil")
        admission = read("app/pi/admission.sigil")
        body = splice(body, "// AUTHORITY_HELPERS", region(api, "AUTHORITY_FACTS") + region(api, "SAME_TOOLS"))
        body = splice(body, "// AUTHORITY_LEXERS", "".join(region(decoder, name) for name in
                      ("WHITESPACE_HELPER", "STRING_END_HELPER", "IDENTIFIER_HELPER")))
        body = splice(body, "// OPERATION_IDENTITY", region(api, "OPERATION_IDENTITY"))
        body = splice(body, "// STATE_CODEC", region(turn, "STATE_CODEC"))
        body = splice(body, "// PROFILE_HELPER", region(admission, "PROFILE_HELPER"))
        body = splice(body, "// FILTER_CONFIG", region(admission, "FILTER_CONFIG"))
        if dispatch or preclaim:
            body = splice(body, "// MODEL_BODY", region(turn, "MODEL_BODY"))
            body = splice(body, "// MODEL_ALLOWANCE", read("app/pi/model_allowance.sigil"))
        if dispatch:
            body = splice(body, "// READ_PATH", region(read("app/pi/read_request.sigil"), "READ_PATH"))
        body = splice(body, "// TURN_HELPERS", read("app/pi/turn_helpers.sigil"))
        body = splice(body, "// RECORD_HELPERS", read("app/shared/record_helpers.sigil"))
        store = read("app/shared/store_protocol.sigil")
        if dispatch:
            body = splice(body, "// STORE_READ_HELPERS", region(store, "STORE_READ_HELPERS"))
        else:
            body = splice(body, "// STORE_PROTOCOL", store)
            body = splice(body, "// OPERATION_RECORDS", read("app/pi/operation_records.sigil"))
        body = splice(body, "// UTF8_VALIDATOR", read("app/shared/utf8.sigil"))
        modules = ["json"]
    elif name == "admission":
        body = read("app/pi/admission.sigil")
        turn = read("app/pi/turn.sigil")
        body = splice(body, "// START_IMPLEMENTATION", region(turn, "TURN_START_HELPERS") + region(turn, "TURN_START"))
        body = splice(body, "// OPERATION_IDENTITY", region(read("app/pi/api.sigil"), "OPERATION_IDENTITY"))
        body = splice(body, "// OPERATION_RECORDS", read("app/pi/operation_records.sigil"))
        body = splice(body, "// TURN_HELPERS", read("app/pi/turn_helpers.sigil"))
        body = splice(body, "// RECORD_HELPERS", read("app/shared/record_helpers.sigil"))
        body = splice(body, "// STORE_PROTOCOL", read("app/shared/store_protocol.sigil"))
        body = splice(body, "// UTF8_VALIDATOR", read("app/shared/utf8.sigil"))
        modules = ["json"]
    elif name == "api":
        body = read("app/pi/api.sigil")
        body = splice(body, "// CANCEL_RECORDS", read("app/pi/cancel_records.sigil"))
        body = splice(body, "// CANCEL_API", read("app/pi/cancel_api.sigil"))
        body = splice(body, "// HISTORY_API", read("app/pi/history_api.sigil"))
        decoder = read("app/pi/submission.sigil")
        begin, end = "// SUBMISSION_DECODER_BEGIN", "// SUBMISSION_DECODER_END"
        if decoder.count(begin) != 1 or decoder.count(end) != 1 or decoder.index(begin) >= decoder.index(end):
            raise ValueError("expected exactly one ordered submission decoder region")
        decoder = decoder.split(begin)[1].split(end)[0]
        decoder = splice(decoder, "// UTF8_VALIDATOR", "")
        body = splice(body, "// SUBMISSION_DECODER", decoder)
        body = splice(body, "// RECORD_HELPERS", read("app/shared/record_helpers.sigil"))
        body = splice(body, "// STORE_PROTOCOL", read("app/shared/store_protocol.sigil"))
        body = splice(body, "// OPERATION_RECORDS", read("app/pi/operation_records.sigil"))
        body = splice(body, "// UTF8_VALIDATOR", read("app/shared/utf8.sigil"))
        modules = ["json"]
    elif name == "delivery_state":
        body = read("app/shared/delivery_state.sigil")
        modules = []
    elif name in {"executor_transaction", "worker_completion"}:
        body = read("app/shared/executor_transaction.sigil")
        entry = (read("app/shared/worker_completion.sigil") if name == "worker_completion" else
                 "pub fn tool_main(input_ptr: i64, input_len: i64) -> i64 @Internal ! { Alloc } {\n"
                 "    return apply_executor(input_ptr, input_len);\n}\n")
        body = splice(body, "// EXECUTOR_ENTRYPOINT", entry)
        kernel = read("app/shared/delivery_state.sigil")
        begin, end = "// DELIVERY_KERNEL_BEGIN", "// DELIVERY_KERNEL_END"
        if kernel.count(begin) != 1 or kernel.count(end) != 1 or kernel.index(begin) >= kernel.index(end):
            raise ValueError("expected exactly one ordered delivery kernel region")
        body = splice(body, "// DELIVERY_KERNEL", kernel.split(begin)[1].split(end)[0])
        body = splice(body, "// STORE_PROTOCOL", read("app/shared/store_protocol.sigil"))
        body = splice(body, "// RECORD_HELPERS", read("app/shared/record_helpers.sigil"))
        body = splice(body, "// UTF8_VALIDATOR", read("app/shared/utf8.sigil"))
        modules = ["json"]
    else:
        body = read(f"app/pi/{'turn' if name in {'turn_transaction', 'turn_completion'} else name}.sigil")
        if name in {"turn", "read_request", "turn_transaction", "turn_completion"}:
            body = splice(body, "// TURN_HELPERS", read("app/pi/turn_helpers.sigil"))
            body = splice(body, "// RECORD_HELPERS", read("app/shared/record_helpers.sigil"))
        if name == "turn":
            body = splice(body, "// TURN_ENTRYPOINT", (
                "pub fn tool_main(input_ptr: i64, input_len: i64) -> i64 @Internal ! { Alloc } {\n"
                "    return reduce_turn(input_ptr, input_len);\n}\n"))
        elif name in {"turn_transaction", "turn_completion"}:
            transaction = read("app/pi/turn_transaction.sigil")
            if name == "turn_completion":
                marker = "pub fn tool_main(input_ptr: i64, input_len: i64)"
                if transaction.count(marker) != 1:
                    raise ValueError("expected exactly one turn transaction entrypoint")
                transaction = transaction.split(marker)[0] + read("app/pi/turn_completion.sigil")
            body = splice(body, "// TURN_ENTRYPOINT", (
                read("app/shared/store_protocol.sigil") + "\n" + transaction))
        body = splice(body, "// UTF8_VALIDATOR", read("app/shared/utf8.sigil"))
        modules = ["json"]
    composed = compose_with_stdlib(body, modules, stdlib_root)
    return ApplicationSource(strip_line_comments(composed.text, compact_indent=name in {"api", "admission", "history", "listing", "dispatch", "settlement", "turn_completion", "coordinator", "preclaim", "dispatch_cancellable", "preclaim_cancellable"}), inputs, composed.stdlib_hash)
