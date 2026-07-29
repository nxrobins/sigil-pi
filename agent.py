#!/usr/bin/env python3
"""pi agent host (milestone 3) — the dispatch loop over separately-forged tools.

Every step is its own ephemeral forge with its own minimal grant manifest:

    llm call   tools/agent_turn.sigil   net: [api host]          (outer ring)
    parse      tools/parse_reply.sigil  no grants — pure         (inner ring)
    dispatch   tools/<name>.sigil       manifest grants only     (per tool)

The ring bridge is the two-forge design: the outer tool never parses JSON,
the inner parser never touches the network — the type system keeps both
honest, and the host just moves bytes between forges.

Frames from parse_reply: tag ('t'/'u'/'?') + 8-digit length + payload;
'u' payload = id \\x1f name \\x1f raw-input-JSON.
"""
import json
import os
import sys
from pathlib import Path

PI_ROOT = Path(__file__).resolve().parent
SIGIL_ROOT = Path(os.environ.get("SIGIL_ROOT", PI_ROOT.parent / "SIGIL")).resolve()
sys.path.insert(0, str(SIGIL_ROOT / "bench" / "src"))

from sigil_bench.compose import compose_with_stdlib  # noqa: E402
from sigil_bench.mcp_client import SigilMCP  # noqa: E402

MAX_STEPS = 8


def decode_frames(data: bytes):
    """Frame stream -> [("text", str) | ("tool_use", id, name, input_dict) |
    ("other", str)] — the host-side half of the bridge protocol."""
    blocks, i = [], 0
    while i < len(data):
        tag = data[i:i + 1]
        n = int(data[i + 1:i + 9])
        payload = data[i + 9:i + 9 + n]
        assert len(payload) == n, "truncated frame"
        i += 9 + n
        if tag == b"t":
            blocks.append(("text", payload.decode()))
        elif tag == b"u":
            tu_id, name, raw_input = payload.split(b"\x1f", 2)
            blocks.append(("tool_use", tu_id.decode(), name.decode(),
                           json.loads(raw_input)))
        else:
            blocks.append(("other", payload.decode()))
    return blocks


class PiAgent:
    def __init__(self, endpoint, api_key, manifest_path=None, sandbox=None,
                 model="claude-sonnet-5", max_tokens=1024, mcp=None):
        self.endpoint = endpoint
        self.api_key = api_key
        self.model = model
        self.max_tokens = max_tokens
        self.sandbox = str(sandbox) if sandbox else None
        self.messages = []
        self.grant_log = []  # (tool_name, grants) per dispatch — tests assert minimality
        manifest_path = manifest_path or PI_ROOT / "tools" / "manifest.json"
        self.manifest = json.loads(Path(manifest_path).read_text())
        self._mcp = mcp
        self._llm_src = compose_with_stdlib(
            (PI_ROOT / "tools" / "agent_turn.sigil").read_text(), ["http"], SIGIL_ROOT).text
        self._parse_src = compose_with_stdlib(
            (PI_ROOT / "tools" / "parse_reply.sigil").read_text(), ["json"], SIGIL_ROOT).text
        self._host = self.endpoint.split("//")[1].split("/")[0].split(":")[0]

    # ── forge plumbing ──────────────────────────────────────────────────

    def _forge(self, source, input_text, grants, fuel=20_000_000):
        r = self._mcp.forge(source, input=input_text, fuel=fuel, grants=grants)
        if r.get("status") != "ok":
            d = (r.get("diagnostics") or [{}])[0]
            return None, f"{d.get('code')}: {(d.get('message') or '')[:200]}"
        return r["data"]["output_text"], None

    def _llm(self, payload: dict):
        hdrs = "\n".join([
            f"x-api-key: {self.api_key}",
            "anthropic-version: 2023-06-01",
            "content-type: application/json",
        ])
        body = json.dumps(payload, ensure_ascii=False)
        out, err = self._forge(
            self._llm_src, f"{self.endpoint}|{hdrs}|{body}",
            {"net": [self._host]})
        if err:
            raise RuntimeError(f"llm forge failed: {err}")
        return out

    def _parse(self, raw_response: str):
        out, err = self._forge(self._parse_src, raw_response, None)
        if err:
            raise RuntimeError(f"parse forge failed: {err}")
        return decode_frames(out.encode())

    # ── tool dispatch ───────────────────────────────────────────────────

    def _grants_for(self, entry):
        grants = {}
        for kind, values in entry.get("grants", {}).items():
            grants[kind] = [v.replace("{SANDBOX}", self.sandbox or "") for v in values]
        return grants or None

    def _dispatch(self, name, tool_input):
        entry = self.manifest.get(name)
        if entry is None:
            return f"unknown tool: {name}", True
        try:
            args = [str(tool_input[a]) for a in entry["args"]]
        except KeyError as e:
            return f"missing tool argument: {e}", True
        source = (PI_ROOT / entry["source"]).read_text()
        grants = self._grants_for(entry)
        self.grant_log.append((name, grants))
        out, err = self._forge(source, "|".join(args), grants)
        if err:
            return err, True
        return out, False

    def tool_specs(self):
        return [e["spec"] for e in self.manifest.values()]

    # ── the loop ────────────────────────────────────────────────────────

    def turn(self, user_message: str) -> str:
        self.messages.append({"role": "user", "content": user_message})
        for _ in range(MAX_STEPS):
            payload = {
                "model": self.model,
                "max_tokens": self.max_tokens,
                "messages": self.messages,
            }
            if self.manifest:
                payload["tools"] = self.tool_specs()
            blocks = self._parse(self._llm(payload))

            assistant_content = []
            tool_results = []
            texts = []
            for block in blocks:
                if block[0] == "text":
                    texts.append(block[1])
                    assistant_content.append({"type": "text", "text": block[1]})
                elif block[0] == "tool_use":
                    _, tu_id, name, tool_input = block
                    assistant_content.append({
                        "type": "tool_use", "id": tu_id,
                        "name": name, "input": tool_input})
                    content, is_error = self._dispatch(name, tool_input)
                    result = {"type": "tool_result", "tool_use_id": tu_id,
                              "content": content}
                    if is_error:
                        result["is_error"] = True
                    tool_results.append(result)
                # "other" blocks are dropped from the conversation

            self.messages.append({"role": "assistant", "content": assistant_content})
            if not tool_results:
                return "\n".join(texts)
            self.messages.append({"role": "user", "content": tool_results})
        raise RuntimeError(f"no final answer after {MAX_STEPS} steps")


def main():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        sys.exit("set ANTHROPIC_API_KEY")
    endpoint = os.environ.get("PI_ENDPOINT", "https://api.anthropic.com/v1/messages")
    sandbox = os.environ.get("PI_SANDBOX", os.getcwd())
    with SigilMCP.spawn(SIGIL_ROOT / "target" / "release" / "sigil-mcp") as mcp:
        mcp.initialize()
        agent = PiAgent(endpoint, api_key, sandbox=sandbox, mcp=mcp,
                        model=os.environ.get("PI_MODEL", "claude-sonnet-5"))
        print(f"pi m3 — {agent.model} @ {endpoint}; sandbox {sandbox}. Ctrl-D exits.")
        while True:
            try:
                msg = input("you> ")
            except EOFError:
                break
            try:
                print("pi >", agent.turn(msg))
            except RuntimeError as e:
                print("pi > [error]", e)


if __name__ == "__main__":
    main()
