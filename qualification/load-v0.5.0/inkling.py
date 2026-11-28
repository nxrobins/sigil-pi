"""Loopback-only adapter for the two Anthropic endpoints used by v0.5.0."""

import http.client
import json
import threading
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

OLLAMA_HOST = "host.docker.internal"
OLLAMA_PORT = 11435
MODEL = "inkling-2-mini"
MODEL_DIGEST = "d183b1321cb57f018c738d2a826d70f575656fbcb7cec67f333d15b0f6a3d178"


def get_json(route):
    conn = http.client.HTTPConnection(OLLAMA_HOST, OLLAMA_PORT, timeout=20)
    try:
        conn.request("GET", route)
        response = conn.getresponse()
        if response.status != 200:
            raise RuntimeError(f"provider metadata HTTP {response.status}")
        return json.loads(response.read())
    finally:
        conn.close()


def identity():
    model = next(model for model in get_json("/api/tags")["models"] if model["name"] == MODEL + ":latest")
    if model["digest"] != MODEL_DIGEST:
        raise RuntimeError("unexpected Inkling model digest")
    return {
        "ollama_version": get_json("/api/version")["version"],
        "model": model["name"],
        "model_digest": model["digest"],
        "bytes": model["size"],
        "details": model["details"],
    }


class Provider(ThreadingHTTPServer):
    request_queue_size = 128
    daemon_threads = False

    def __init__(self):
        super().__init__(("127.0.0.1", 0), Handler)
        self.lock = threading.Lock()
        self.counts = Counter()

    def record(self, event):
        with self.lock:
            self.counts[event] += 1


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        count = self.path == "/v1/messages/count_tokens"
        if not count and self.path != "/v1/messages":
            self.send_error(404)
            return
        kind = "count" if count else "generation"
        self.server.record(kind + "_requests")
        conn = http.client.HTTPConnection(OLLAMA_HOST, OLLAMA_PORT, timeout=125)
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 16 * 1024 * 1024:
                raise ValueError("invalid request size")
            body = self.rfile.read(size)
            request = json.loads(body)
            if request["model"] != MODEL or request.get("stream"):
                raise ValueError("unexpected model or streaming request")
            if count:
                # v0.5.0 removes only max_tokens from its generation payload.
                # Preserve the entire prompt, chat template inputs and thinking
                # setting. The upstream usage is measured with a one-token cap.
                request["max_tokens"] = 1
                body = json.dumps(request, ensure_ascii=False).encode()
            conn.request(
                "POST",
                "/v1/messages",
                body=body,
                headers={"Content-Type": "application/json", "anthropic-version": "2023-06-01"},
            )
            response = conn.getresponse()
            status = response.status
            body = response.read()
            self.server.record(f"{kind}_upstream_http_{status}")
            if 200 <= status < 300:
                message = json.loads(body)
                if message["model"].removesuffix(":latest") != MODEL:
                    raise ValueError("unexpected upstream reply model")
                if count:
                    tokens = message["usage"]["input_tokens"]
                    if type(tokens) is not int or tokens <= 0:
                        raise ValueError("missing measured input tokens")
                    body = json.dumps({"input_tokens": tokens}).encode()
                else:
                    self.server.record("generation_stop_" + str(message.get("stop_reason")))
        except (OSError, http.client.HTTPException, ValueError, KeyError, TypeError) as error:
            self.server.record(kind + "_error_" + type(error).__name__)
            status, body = 502, b'{"error":{"type":"api_error","message":"local provider adapter failed"}}'
        finally:
            conn.close()
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            self.server.record(kind + "_client_disconnected")
