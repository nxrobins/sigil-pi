"""Explicit HTTP build profile. No request dispatcher or runtime Python policy."""
from dataclasses import dataclass
import hashlib
from pathlib import Path

from scripts.compose_application import compose_application, strip_line_comments

BASELINE = "ec1d1bf060609fc0a166d49325e76f3f63b5e45070a24faa5a2d72474150d34b"
HEADER_NAMES = ["retry-after", "server-timing", "www-authenticate", "x-request-id",
                "x-sigil-retries", "x-sigil-tool-calls"]


@dataclass(frozen=True)
class HttpEntrySource:
    text: str
    input_hashes: dict[str, str]
    stdlib_hash: str

    @property
    def compiler_input_sha256(self):
        return hashlib.sha256(self.text.encode()).hexdigest()


def compose_http_entry(repo, stdlib):
    base = compose_application("api_discovery", stdlib, root=repo)
    if base.compiler_input_sha256 != BASELINE:
        raise ValueError("HTTP profile baseline changed; review/version before rebasing")
    inputs = dict(base.input_hashes)
    def read(name):
        relative = "app/pi/" + name
        raw = (Path(repo) / relative).read_bytes()
        inputs[relative] = hashlib.sha256(raw).hexdigest()
        return raw.decode()

    policy = read("http_request_id.sigil")
    begin, end = "// REQUEST_ID_POLICY_BEGIN", "// REQUEST_ID_POLICY_END"
    if policy.count(begin) != 1 or policy.count(end) != 1 or policy.index(begin) >= policy.index(end):
        raise ValueError("request-ID policy region is not unique and ordered")
    policy = policy.split(begin)[1].split(end)[0]
    body = base.text

    def replace(old, new, count):
        nonlocal body
        if body.count(old) != count:
            raise ValueError(f"expected {count} reviewed HTTP ABI sites: {old}")
        body = body.replace(old, new)

    replace('"AH4\\n", 13', '"AH5\\n", 15', 2)
    replace('"HC4\\n"', '"HC5\\n"', 8)
    # Metadata validation gives the following call an Internal control context.
    # Strengthen BOTH internal callees, not just the wrapper, so that no step in
    # this call chain requires a downgrade. No declassification is introduced.
    replace("fn api_decision(input_ptr: i64, input_len: i64)",
            "fn api_decision(input_ptr: i64 @Internal, input_len: i64 @Internal)", 1)
    replace("pub fn tool_main(input_ptr: i64, input_len: i64)",
            "fn api_response_core(input_ptr: i64 @Internal, input_len: i64 @Internal)", 1)
    body += policy + read("http_api.sigil")
    inputs["scripts/compose_http_entry.py"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    return HttpEntrySource(strip_line_comments(body, compact_indent=True), inputs, base.stdlib_hash)
