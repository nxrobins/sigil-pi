"""Public memory and HTTP behavior against local sidecar and embedding peers."""
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from agent import LocalEmbeddingClient, PiAgent, PiMemory, SessionStore, serve
from test_sse_progress import _sse_request


PEER = r'''
import json, sys, time
mode, log = sys.argv[1:3]
model = sys.argv[sys.argv.index("--embedding-model") + 1]
inspections = {}
held = False

def send(obj):
    print(json.dumps(obj), flush=True)

for line in sys.stdin:
    req = json.loads(line)
    with open(log, "a") as file:
        file.write(line)
    op, rid = req.get("op"), req.get("id")
    if op == "hello":
        result = {"protocol": 2, "embedder": {"mode": "callback", "model_id": model},
            "fact_grounding": {"enabled": True, "roles": ["fact_support", "fact_revision"],
            "min_confidence": 0.8, "quarantine": True, "replay_barriers": True,
            "extraction_enabled": True, "current_fact_retrieval_enabled": True}}
    elif op == "probe_embedding":
        result = {"dimensions": 2}
    elif op == "record":
        if mode == "slow_records" or (mode == "slow_action" and req["kind"] == "agent_action"):
            time.sleep(0.08 if mode == "slow_records" else 2)
        if mode == "partial_record":
            sys.stdout.write('{"id":')
            sys.stdout.flush()
            time.sleep(5)
        result = {"cycle": rid}
    elif op == "retrieve":
        if mode == "embedding":
            send({"callback": "embedding_request", "call_id": 7, "model": model, "texts": [req["query"]]})
            answer = sys.stdin.readline()
            with open(log, "a") as file:
                file.write(answer)
        result = {"episodic": [{"text": "Nadia used to live in Oslo"}]}
    elif op == "inspect_facts":
        session = req["session"]
        inspections[session] = inspections.get(session, 0) + 1
        target = {"subject": "nadia", "predicate": "lives in", "object": "Oslo"}
        if mode == "hold_busy" and inspections[session] > 1:
            send({"id": rid, "err": {"code": "busy", "message": "review holds the record"}})
            continue
        result = {"current": [], "pending": [{"target": target}]} if held else {"current": [target]}
    elif op == "consolidate_all" and mode == "held_review":
        held = True
        send({"callback": "model_request", "call_id": 8, "request": {"role": "Consolidate"}})
        answer = sys.stdin.readline()
        with open(log, "a") as file:
            file.write(answer)
        result = {"reports": []}
    elif op == "shutdown":
        send({"id": rid, "ok": {}})
        break
    else:
        result = {}
    send({"id": rid, "ok": result})
'''


def memory_peer(tmp_path, mode="normal", embed=None):
    peer = tmp_path / "peer.py"
    log = tmp_path / "peer.jsonl"
    peer.write_text(PEER)
    memory = PiMemory([sys.executable, str(peer), mode, str(log)], tmp_path / "memory",
                      fact_grounding=True, complete=lambda r: {}, judge=lambda r: {},
                      embed=embed or (lambda texts, model, **kwargs: [[1.0, 0.0] for _ in texts]))
    return memory, log


def make_agent(tmp_path, memory, deadline=None):
    agent = PiAgent("http://localhost/v1/messages", "unused", SessionStore(tmp_path / "sessions"),
                    tmp_path / "sandboxes", memory=memory, turn_deadline_s=deadline)
    agent._llm = lambda *args, **kwargs: "raw"
    agent._parse = lambda *args, **kwargs: [("text", "Nadia lives in Oslo.")]
    return agent


def requests(log):
    return [json.loads(line) for line in log.read_text().splitlines()]


def chat(server, payload, stream):
    if stream:
        return _sse_request(server, payload)[-1]
    req = urllib.request.Request(f"http://127.0.0.1:{server.server_address[1]}/chat",
        data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            return "reply", json.load(response)
    except urllib.error.HTTPError as error:
        return "error", json.load(error)


@pytest.mark.parametrize("stream", [False, True])
def test_recall_known_owner_then_busy_review_cannot_deliver_stale_draft(tmp_path, stream):
    memory, log = memory_peer(tmp_path, "held_review")
    memory.recall_lock_timeout = 0.05
    agent = make_agent(tmp_path, memory)
    prompts = []
    reviewing, release = threading.Event(), threading.Event()
    review_thread = threading.Thread(target=memory.consolidate_now)

    def complete(request):
        reviewing.set()
        assert release.wait(timeout=5)
        return {"unavailable": True}

    def draft(payload, **kwargs):
        prompts.append(payload["system"])
        review_thread.start()
        assert reviewing.wait(timeout=2)
        return "raw"

    memory.complete = complete
    agent._llm = draft
    server = serve(agent, port=0)
    try:
        event, result = chat(server, {"session": "s", "message": "Where does Nadia live?"}, stream)
        assert event == "reply"
        assert "Oslo" in prompts[0]
        assert result["reply"] == "Verified memory is unavailable; I cannot confirm a current residence."
        assert result["memory_receipt"]["recorded"] is True
        assert agent.store.load("s")[-1]["content"][0]["text"] == result["reply"]
        assert memory._pending[-1]["text"] == result["reply"]
        release.set()
        review_thread.join(timeout=2)
        assert not review_thread.is_alive()
        assert memory.inspect_facts("s")["current"] == []
        memory.record("s", "flush", "user_message")
        assert [r for r in requests(log) if r.get("kind") == "agent_action"][-1]["text"] == result["reply"]
    finally:
        release.set()
        if review_thread.ident is not None:
            review_thread.join(timeout=2)
        server.shutdown()
        server.server_close()
        memory.stop()


@pytest.mark.parametrize("query,inspections", [
    ("Hello", 0),
    ("Where does Nadia's sister live?", 0),
    ("What is Nadia’s sister's residence?", 0),
    ("What is Nadia's residence as of 2020?", 0),
    ("Where does Nadia live? Explain Oslo's climate.", 0),
    ("Nadia lives in Oslo. Compare it with Bergen.", 0),
    ("Where does Unknown Person live?", 1),
])
def test_ordinary_questions_skip_unneeded_inspection(tmp_path, query, inspections):
    memory, log = memory_peer(tmp_path)
    agent = make_agent(tmp_path, memory)
    try:
        assert agent.turn("s", query) == "Nadia lives in Oslo."
        assert len([r for r in requests(log) if r.get("op") == "inspect_facts"]) == inspections
        assert not vars(memory._turn_context)
    finally:
        memory.stop()


def test_owner_qualification_is_local_to_concurrent_and_nested_turns(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    memory, log = memory_peer(tmp_path, "hold_busy")
    agent = make_agent(tmp_path, memory)
    barrier = threading.Barrier(2)

    def draft(payload, session=None):
        barrier.wait(timeout=3)
        if session == "known":
            assert memory._turn_context.owner == "nadia"
            with memory.turn_context(time.monotonic() + 5):
                memory.recall("nested", "Where does Unknown Person live?")
                assert memory._turn_context.owner is None
            assert memory._turn_context.owner == "nadia"
        else:
            assert memory._turn_context.owner is None
        return "raw"

    agent._llm = draft
    try:
        with ThreadPoolExecutor(2) as pool:
            known = pool.submit(agent.turn, "known", "Where does Nadia live?")
            unknown = pool.submit(agent.turn, "unknown", "Where does Unknown Person live?")
            assert "unavailable" in known.result(timeout=5)
            assert unknown.result(timeout=5) == "Nadia lives in Oslo."
        assert not vars(memory._turn_context)
    finally:
        memory.stop()


@pytest.mark.parametrize("guard", ["_pending_guard", "_lock"])
def test_memory_locks_share_the_turn_budget_and_report_unsaved_evidence(tmp_path, guard):
    memory, log = memory_peer(tmp_path)
    agent = make_agent(tmp_path, memory, deadline=0.15)
    getattr(memory, guard).acquire()
    start = time.monotonic()
    try:
        with pytest.raises(TimeoutError, match="deadline"):
            agent.turn("s", "Correction: Nadia no longer lives in Oslo.")
        assert time.monotonic() - start < 0.7
        receipt = agent.memory_delivery_receipt()
        assert receipt["recorded"] is False
        assert receipt["status"] == ("record_failed" if guard == "_pending_guard" else "buffered")
        assert receipt["reason"] == "deadline"
        assert agent.store.load("s")[0]["content"].startswith("Correction:")
        assert not vars(memory._turn_context)
    finally:
        getattr(memory, guard).release()
        memory.stop()


def test_pending_flush_never_renews_the_remaining_deadline(tmp_path):
    memory, log = memory_peer(tmp_path, "slow_records")
    memory._pending = [{"op": "record", "session": "old", "text": str(i), "kind": "user_message"} for i in range(6)]
    try:
        start = time.monotonic()
        with memory.turn_context(start + 0.22):
            receipt = memory.record("s", "new correction", "user_message")
        assert time.monotonic() - start < 0.7
        assert receipt["status"] == "buffered" and receipt["recorded"] is False
        assert receipt["reason"] == "deadline"
        assert memory._pending[-1]["text"] == "new correction"
        sent = [r for r in requests(log) if r.get("op") == "record"]
        assert 1 <= len(sent) < 7
        memory._proc.wait(timeout=2)
    finally:
        memory.stop()


def test_partial_sidecar_response_cannot_outlive_the_turn(tmp_path):
    memory, log = memory_peer(tmp_path, "partial_record")
    agent = make_agent(tmp_path, memory, deadline=0.15)
    start = time.monotonic()
    try:
        with pytest.raises(TimeoutError, match="deadline"):
            agent.turn("s", "Correction: Nadia no longer lives in Oslo.")
        assert time.monotonic() - start < 0.7
        assert agent.memory_delivery_receipt()["status"] == "buffered"
        assert agent.store.load("s")[0]["content"].startswith("Correction:")
        memory._proc.wait(timeout=2)
    finally:
        memory.stop()


@pytest.mark.parametrize("stream", [False, True])
def test_slow_assistant_record_keeps_user_receipt_and_fails_delivery(tmp_path, stream):
    memory, log = memory_peer(tmp_path, "slow_action")
    agent = make_agent(tmp_path, memory, deadline=0.15)
    server = serve(agent, port=0)
    try:
        event, result = chat(server, {"session": "s", "message": "Correction: Nadia no longer lives in Oslo."}, stream)
        assert event == "error" and "deadline" in result["error"]
        assert result["memory_receipt"]["recorded"] is True
        assert result["memory_receipt"]["status"] == "awaiting_review"
        assert len(agent.store.load("s")) == 1
        assert memory._pending[-1]["kind"] == "agent_action"
    finally:
        server.shutdown()
        server.server_close()
        memory.stop()


@pytest.mark.parametrize("phase", ["headers", "slow_headers", "body", "drip", "success"])
def test_embedding_callback_obeys_the_same_external_turn_deadline(tmp_path, phase):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            body = json.dumps({"data": [{"index": 0, "embedding": [1.0, 0.0]}]}).encode()
            try:
                time.sleep(0.02 if phase != "headers" else 0.4)
                if phase == "slow_headers":
                    header = f"HTTP/1.0 200 OK\r\nContent-Length: {len(body)}\r\n\r\n".encode()
                    for byte in header:
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                        time.sleep(0.03)
                else:
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                if phase == "body":
                    time.sleep(0.4)
                if phase == "drip":
                    for byte in body:
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                        time.sleep(0.03)
                else:
                    self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *args):
            pass

    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    client = LocalEmbeddingClient(f"http://127.0.0.1:{http.server_port}/embeddings", "bge-small-en-v1.5", timeout_s=10)
    memory, log = memory_peer(tmp_path, "embedding", client.embed)
    agent = make_agent(tmp_path, memory, deadline=5)
    start = time.monotonic()
    try:
        if phase == "success":
            assert agent.turn_with_usage("s", "hello", deadline_monotonic=start + 0.3)[0] == "Nadia lives in Oslo."
        else:
            with pytest.raises(TimeoutError, match="deadline"):
                agent.turn_with_usage("s", "hello", deadline_monotonic=start + 0.15)
        assert time.monotonic() - start < 0.7
        assert agent.memory_delivery_receipt()["recorded"] is True
        assert agent.store.load("s")[0]["content"] == "hello"
    finally:
        memory.stop()
        http.shutdown()
        http.server_close()
