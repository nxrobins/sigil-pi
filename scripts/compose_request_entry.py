"""Build-only explicit v8 application composition, never a Python dispatcher."""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from scripts.compose_application import ApplicationSource, strip_line_comments
from scripts.compose_http_entry import compose_http_entry
from scripts.sigil_layout import compact_layout

BASELINE = "4261e3bcb64f0b7b6f80f5e0226336cc6ff5d3c020457b4af4724bd8f327e289"


@dataclass(frozen=True)
class RequestEntrySource(ApplicationSource):
    # Only the checked compact .text is an executable compiler input. This
    # provenance text exists to compare token streams with the pinned lexer.
    layout_input: str


def compose_request_entry(repo, stdlib, *, request_limit):
    baseline_root = fragment_root = Path(repo)
    limit = request_limit
    # Mandatory owner/build input. There is deliberately no pilot default.
    if type(limit) is not int or not 1 <= limit <= 9223372036854775807:
        raise ValueError("explicit request limit must be a positive signed-64-bit integer")
    baseline = compose_http_entry(baseline_root, stdlib)
    if baseline.compiler_input_sha256 != BASELINE:
        raise ValueError("request entry requires the reviewed v7 source fingerprint")
    inputs = dict(baseline.input_hashes)

    def read(root, name):
        raw = (Path(root) / name).read_bytes()
        inputs[name] = hashlib.sha256(raw).hexdigest()
        return raw.decode()

    def region(source, name):
        begin, end = f"// {name}_BEGIN", f"// {name}_END"
        if source.count(begin) != 1 or source.count(end) != 1 or source.index(begin) >= source.index(end):
            raise ValueError(f"expected one ordered {name} region")
        return source.split(begin)[1].split(end)[0]

    def compact(source):
        return strip_line_comments(source, compact_indent=True)

    wrapper = compact(read(baseline_root, "app/pi/http_api.sigil"))
    if not baseline.text.endswith(wrapper):
        raise ValueError("original HTTP wrapper must be a complete exact suffix")
    body = baseline.text[:-len(wrapper)]

    def replace(old, new, count=1):
        nonlocal body
        if body.count(old) != count:
            raise ValueError(f"expected {count} explicit v8 composition sites: {old[:100]}")
        body = body.replace(old, new)

    decoder_source = read(baseline_root, "app/pi/submission.sigil")
    decoder = region(decoder_source, "SUBMISSION_DECODER")
    if decoder.count("// UTF8_VALIDATOR") != 1:
        raise ValueError("expected original decoder UTF-8 placeholder")
    # The old nested recipes compact twice: the second pass removes only the
    # leading spaces left where comments were stripped on the first pass.
    decoder = compact(compact(decoder.replace("// UTF8_VALIDATOR", "")))
    lexers = compact("".join(region(decoder_source, name) for name in
                            ("WHITESPACE_HELPER", "STRING_END_HELPER", "IDENTIFIER_HELPER")))
    replace(decoder, lexers)
    replace('"AH5\\n", 15', '"AH6\\n", 17', 2)
    replace('"HC5\\n"', '"HC6\\n"', 8)
    replace('arr_len(get(x, 10)) != 3', 'arr_len(get(x, 10)) != 4')
    replace('!is_text(item(get(x, 10), 2), "listing")',
            '!is_text(item(get(x, 10), 2), "listing") || !is_text(item(get(x, 10), 3), "request_policy")')
    old_commands = '["call","commit","metadata","read","reply"]'
    new_commands = '["call","commit","metadata","read","read_many","reply"]'
    replace(json.dumps(old_commands), json.dumps(new_commands))
    replace('canonical = decode_submission(ptr(get(x, 2)), size(get(x, 2)));',
            'canonical = prepared_submission(ptr(get(x, 2)), size(get(x, 2)));')
    replace('if submitting { return command("read", get(c, 7), key, text("dedup")); }',
            'if submitting { return grouped_begin(c, request, key); }')
    read_boundary = ('if !is_text(get(x, 6), "read") { return -400; }\n'
                     'let found: i64 @Internal = read_record(get(x, 7), "SR1\\n", 3);')
    replace(read_boundary,
            'if submitting && is_text(get(x, 6), "read_many") { return grouped_seen(x, c, request, key); }\n'
            + read_boundary)
    # Replace exactly the old submission-only continuation tail; polling,
    # cancellation, history, discovery and the real admission result stay intact.
    original = read(baseline_root, "app/pi/api.sigil")
    start = '    if starts(get(x, 8), "AS1\\n") {'
    end = '\n}\n\npub fn tool_main('
    if original.count(start) != 1 or original.count(end) != 1:
        raise ValueError("original submission continuation region is not unique")
    tail = original.split(start)[1].split(end)[0]
    replace(compact(compact(start + tail)), 'return grouped_previous(x, c, key);')
    shared = read(fragment_root, "app/shared/read_set.sigil")
    grouped = read(fragment_root, "app/pi/request_grouped.sigil")
    replace('fn api_decision(', compact(shared + grouped) + '\nfn api_decision(')
    entry = read(fragment_root, "app/pi/request_entry.sigil")
    if entry.count('"// REQUEST_LIMIT_LITERAL"') != 1:
        raise ValueError("request limit needs one explicit SIGIL literal site")
    entry = entry.replace('"// REQUEST_LIMIT_LITERAL"', json.dumps(str(limit)))
    layout_input = compact(body + entry)
    body = compact_layout(layout_input)
    if body.count("pub fn tool_main(") != 1 or len(body.encode()) > 65536:
        raise ValueError(f"v8 entry must fit original entry/source ceilings: {len(body.encode())} bytes")
    inputs["scripts/compose_request_entry.py"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    inputs["scripts/sigil_layout.py"] = hashlib.sha256((Path(__file__).parent / "sigil_layout.py").read_bytes()).hexdigest()
    inputs["configuration/request-limit"] = hashlib.sha256(str(limit).encode()).hexdigest()
    return RequestEntrySource(body, inputs, baseline.stdlib_hash, layout_input)
