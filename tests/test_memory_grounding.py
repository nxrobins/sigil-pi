"""Typed proof boundary tests, separate from the opt-in live evaluation."""
import copy
import threading
import pytest
from memory_judgment import build_body, parse_response


def request():
    return {"role": "Judge", "state": {"task": "verify_fact_support", "evidence": "X does not live in Y"},
            "questions": {"q": {"type": "choice", "instructions": "supported?",
                "options": [{"key": "yes"}, {"key": "no"}]}}}


def response():
    return {"answers": {"q": {"type": "choice", "choice": "yes", "confidence": 0.79,
                              "probabilities": {"yes": 0.79, "no": 0.21}}}}


def test_preserves_complete_evidence_and_native_below_floor_score():
    req = request()
    assert build_body(req, "jev-latest")["state"] == req["state"]
    assert parse_response(response(), req)["answers"]["q"]["confidence"] == 0.79


@pytest.mark.parametrize("role,task", [("Recall", "verify_fact_support"), ("Judge", "coreference"), ("Extract", "review_stored_fact")])
def test_rejects_other_roles_before_transport(role, task):
    req = request(); req["role"] = role; req["state"]["task"] = task
    with pytest.raises(ValueError): build_body(req, "jev-latest")


@pytest.mark.parametrize("broken", ["missing", "extra", "type", "distribution", "nan", "unknown"])
def test_incomplete_or_malformed_responses_cannot_be_proofs(broken):
    data = copy.deepcopy(response()); answer = data["answers"]["q"]
    if broken == "missing": data["answers"] = {}
    elif broken == "extra": data["answers"]["extra"] = answer
    elif broken == "type": answer["type"] = "noul"
    elif broken == "distribution": answer["probabilities"].pop("no")
    elif broken == "nan": answer["confidence"] = float("nan")
    elif broken == "unknown": answer["choice"] = "unknown"
    with pytest.raises((ValueError, KeyError)): parse_response(data, request())


def test_fact_judgment_uses_audited_secret_injected_forge(scripted_llm, tmp_path, mcp):
    from conftest import make_agent
    from agent import MEMORY_SESSION, verify_audit_dir
    from memory_judgment import FactJudgment
    agent = make_agent(scripted_llm, tmp_path, mcp)
    scripted_llm.script = [response()]
    provider = FactJudgment(agent, 'synthetic-test-key', endpoint=scripted_llm.url)
    result = provider(request())
    assert result['answers']['q']['confidence'] == 0.79
    assert scripted_llm.requests[-1]['state'] == request()['state']
    records = agent.audit.read(MEMORY_SESSION)
    assert records[-1]['kind'] == 'fact_support'
    assert 'synthetic-test-key' not in str(records)
    assert verify_audit_dir(agent.audit.dir)['ok']
    assert records[-1]['grants']['secret'] == ['jev=<redacted>']



def test_memory_subprocess_does_not_inherit_provider_credentials(tmp_path, monkeypatch):
    import subprocess
    from test_memory import make_memory
    original = subprocess.Popen
    seen = []
    def spawn(*args, **kwargs):
        seen.append(kwargs.get("env"))
        return original(*args, **kwargs)
    monkeypatch.setenv("TYPESAFE_API_KEY", "synthetic-jev-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-chat-key")
    monkeypatch.setenv("PI_AUDIT_KEY", "synthetic-audit-key")
    monkeypatch.setenv("PI_AUTH_TOKEN", "synthetic-auth-token")
    monkeypatch.setenv("PI_SECRET_EXAMPLE", "synthetic-secret")
    monkeypatch.setattr(subprocess, "Popen", spawn)
    memory = make_memory(tmp_path, monkeypatch)
    try:
        assert seen and all(isinstance(env, dict) for env in seen)
        for env in seen:
            assert not any(k.endswith("_API_KEY") or k.startswith("PI_SECRET_")
                           or k in ("PI_AUDIT_KEY", "PI_AUTH_TOKEN") for k in env)
    finally:
        memory.stop()


@pytest.mark.parametrize("kind,expected", [("llm", True), ("fact_support", True), ("fact_revision", True), ("tool", False), ("parse", False)])
def test_provider_allowance_is_role_scoped_and_audited(tmp_path, kind, expected):
    from agent import PiAgent, SessionStore
    class Runtime:
        def forge(self, source, **kwargs):
            self.kwargs = kwargs
            return {"status": "ok", "data": {"output_text": "ok"}}
    runtime = Runtime()
    agent = PiAgent("http://localhost/v1/messages", "synthetic-key",
                    store=SessionStore(tmp_path / "sessions"), sandbox_root=tmp_path / "sandboxes",
                    mcp=runtime, provider_timeout_ms=30000)
    grants = {"net": ["localhost"], "secret": ["test=synthetic-key"]}
    agent._forge("source", "input", grants, kind=kind, session="s")
    assert ("http_timeout_ms" in runtime.kwargs["grants"]) is expected
    assert "http_timeout_ms" not in grants
    receipt = agent.audit.read("s")[-1]
    assert "synthetic-key" not in str(receipt)
    if expected:
        assert receipt["grants"]["http_timeout_ms"] == 30000


def test_provider_allowance_respects_remaining_turn_budget(tmp_path):
    import time
    from agent import PiAgent, SessionStore
    class Runtime:
        def forge(self, source, **kwargs):
            self.kwargs = kwargs
            return {"status": "ok", "data": {"output_text": "ok"}}
    runtime = Runtime()
    agent = PiAgent("http://localhost/v1/messages", "key", store=SessionStore(tmp_path / "sessions"),
                    sandbox_root=tmp_path / "sandboxes", mcp=runtime, provider_timeout_ms=30000)
    agent._turn_context.deadline = time.monotonic() + 5
    agent._forge("source", "input", {"net": ["localhost"]}, kind="llm")
    assert 0 < runtime.kwargs["grants"]["http_timeout_ms"] <= 4000
    assert runtime.kwargs["timeout_s"] <= 5


def test_grounded_correction_is_recorded_before_failed_reply(tmp_path):
    from types import SimpleNamespace
    from agent import PiAgent, SessionStore
    records = []
    receipt = {"status": "awaiting_review", "recorded": True, "fact_verified": False, "cycle_number": 1}
    memory = SimpleNamespace(fact_grounding=True, complete=lambda r: {}, judge=lambda r: {},
                             recall=lambda *a: "", record=lambda *a: (records.append(a), receipt)[1])
    agent = PiAgent("http://localhost/v1/messages", "key", store=SessionStore(tmp_path / "sessions"),
                    sandbox_root=tmp_path / "sandboxes", memory=memory)
    def failed(payload, session=None):
        assert records == [("s", "Correction: Rowan no longer lives in Elmport", "user_message")]
        raise RuntimeError("provider unavailable")
    agent._llm = failed
    with pytest.raises(RuntimeError): agent.turn("s", "Correction: Rowan no longer lives in Elmport")
    assert agent.memory_delivery_receipt() == receipt
    assert agent.store.load("s")[0]["content"].startswith("Correction:")


def test_runtime_rejects_provider_allowance_without_advertised_support():
    from runtime_client import ProductionSigilMCP, RuntimeClientError
    client = ProductionSigilMCP(None, None, None)
    calls = []
    client._request = lambda *args, **kw: (calls.append(args), {"tools": []})[1]
    with pytest.raises(RuntimeClientError, match="bounded provider"):
        client.forge("source", grants={"http_timeout_ms": 30000})
    assert calls == [("tools/list",)]



def test_current_context_uses_verified_records_and_preserves_negation_scope():
    from memory_context import current_fact_context
    snap = {"current": [{"subject": "rhea calder", "predicate": "does not live in", "object": "Mapleridge", "source_cycle": 5}],
            "pending": [{"target": {"subject": "rhea calder", "predicate": "lives in", "object": "Cedarhaven"}}]}
    block = current_fact_context(snap, "What is Rhea Calder current residence?")
    assert '"predicate": "does not live in"' in block
    assert '"quarantined"' in block and '"source_cycle": 5' in block
    assert current_fact_context(snap, "Where did Rhea Calder live before?") is None
    assert current_fact_context(snap, "What was Rhea Calder residence in 2020?") is None
    assert current_fact_context(snap, "Where does she live now?") is None
    assert current_fact_context(snap, "Where does Rhea Calderwood live now?") is None
    assert "exceeds" in current_fact_context(snap, "What is Rhea Calder current residence?", max_bytes=200)


def test_grounded_recall_overrides_stale_episodic_results_without_changing_store():
    from agent import PiMemory
    memory = object.__new__(PiMemory)
    memory._turn_context = threading.local()
    memory.fact_grounding = True
    memory.budget = 512
    memory.block_bytes = 8192
    memory.recall_lock_timeout = 2
    calls = []
    def call(request, **kwargs):
        calls.append(request)
        if request["op"] == "retrieve":
            return {"ok": {"episodic": [{"text": "Rhea Calder lives in Cedarhaven"}]}}
        return {"ok": {"current": [{"subject":"rhea calder", "predicate":"lives in", "object":"Mapleridge"}], "pending":[]}}
    memory._call = call
    result = memory.recall("s", "What is Rhea Calder current verified residence?")
    assert "Mapleridge" in result and "Cedarhaven" not in result
    assert [c["op"] for c in calls] == ["retrieve", "inspect_facts"]
    memory._call = lambda *a, **kw: {"err": {"code": "busy"}}
    assert memory.recall("s", "What is Rhea Calder current residence?") == ""
    memory.fact_grounding = False
    assert memory.recall("s", "What is Rhea Calder current residence?") == ""


@pytest.mark.parametrize("message", [
    "Correction: Tessa no longer lives in Brookhaven.",
    "I am no longer certain that Tessa lives in Brookhaven.",
    "Tessa does not live in Brookhaven.",
    "Tessa lives in Brookhaven.",
    "Verified NEW update: Tessa lives in Brookhaven again.",
])
@pytest.mark.parametrize("status,recorded,fragment", [
    ("awaiting_review", True, "saved as evidence"),
    ("buffered", False, "not yet been recorded"),
    ("record_failed", False, "couldn’t save"),
    ("unknown", False, "can’t confirm"),
])
def test_acknowledgement_follows_delivery_not_model_claim(tmp_path, message, status, recorded, fragment):
    from types import SimpleNamespace
    from agent import PiAgent, SessionStore
    records = []
    receipt = {"status": status, "recorded": recorded, "fact_verified": False}
    memory = SimpleNamespace(fact_grounding=True, complete=lambda r: {}, judge=lambda r: {},
        recall=lambda *a: "", record=lambda *a: (records.append(a), receipt)[1])
    agent = PiAgent("http://localhost/v1/messages", "key", store=SessionStore(tmp_path / "sessions"),
                    sandbox_root=tmp_path / "sandboxes", memory=memory)
    agent._llm = lambda *a, **kw: "draft"
    agent._parse = lambda *a, **kw: [("text", "I have verified the residence and withdrawn the previous record.")]
    reply = agent.turn("s", message)
    assert fragment in reply and "I have verified" not in reply
    assert agent.memory_delivery_receipt() == receipt
    assert records == [("s", message, "user_message"), ("s", reply, "agent_action")]
    assert agent.store.load("s")[-1] == {"role": "assistant", "content": [{"type": "text", "text": reply}]}


@pytest.mark.parametrize("message", [
    "What is Tessa current residence?", "Where did Tessa live in 2020?",
    "Tessa lived in Brookhaven.",
    "Tessa lives in Brookhaven. Can you search for its population?",
    "Correction: Tessa lives in Brookhaven. Explain the change.",
    "Write a fictional scene about Tessa's residence.",
    "I feel uncertain about tomorrow.", "Hello there",
    "Nadia lives in Oslo. Compare it with Bergen.",
    "Nadia lives in Oslo and compare it with Bergen.",
    "Correction: Nadia lives in Oslo; compare it with Bergen.",
    "Note: Nadia lives in Oslo\nCompare it with Bergen.",
    "Tessa’s residence uncertainty is resolved. The original record was correct. Explain the change.",
    'In fictional dialogue someone says "Tessa does not live in Brookhaven". This is not a correction.',
])
def test_receipt_renderer_does_not_replace_questions_or_other_work(message):
    from memory_context import memory_update_acknowledgement
    assert memory_update_acknowledgement(message, {"status": "awaiting_review", "recorded": True}) is None


@pytest.mark.parametrize("stream", [False, True])
def test_http_and_stream_deliver_the_receipt_owned_acknowledgement(tmp_path, stream):
    import json
    import urllib.request
    from types import SimpleNamespace
    from agent import PiAgent, SessionStore, serve
    from test_sse_progress import _sse_request
    receipt = {"status": "awaiting_review", "recorded": True, "fact_verified": False, "cycle": 8}
    memory = SimpleNamespace(fact_grounding=True, complete=lambda r: {}, judge=lambda r: {},
        recall=lambda *a: "", record=lambda *a: receipt)
    agent = PiAgent("http://localhost/v1/messages", "key", store=SessionStore(tmp_path / "sessions"),
                    sandbox_root=tmp_path / "sandboxes", memory=memory)
    agent._llm = lambda *a, **kw: "draft"
    agent._parse = lambda *a, **kw: [("text", "I've withdrawn that fact and verified the replacement.")]
    server = serve(agent, port=0)
    payload = {"session": "s", "message": "Correction: Tessa no longer lives in Brookhaven."}
    try:
        if stream:
            result = _sse_request(server, payload)[-1][1]
        else:
            request = urllib.request.Request(f"http://127.0.0.1:{server.server_address[1]}/chat",
                data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(request) as response:
                result = json.load(response)
        assert result["memory_receipt"] == receipt
        assert "saved as evidence" in result["reply"] and "I've withdrawn" not in result["reply"]
        assert agent.store.load("s")[-1]["content"][0]["text"] == result["reply"]
    finally:
        server.shutdown()
        server.server_close()


def test_current_answer_uses_only_the_inspector_preserving_negation_and_scope():
    from memory_context import current_residence_answer
    q = "What is Nera Bloom current verified residence?"
    target = {"subject":"nera bloom", "predicate":"lives in", "object":"Ferryvale"}
    held = {"current":[], "pending":[{"target":target}]}
    assert current_residence_answer(held,q) == "No verified current residence is available for nera bloom."
    negative = dict(target, predicate="does not live in")
    assert "does not live in Ferryvale" in current_residence_answer({"current":[negative]},q)
    historical = dict(target, predicate="lived in during 2020")
    assert "No verified current residence" in current_residence_answer({"current":[historical]},q)
    positive = {"current":[target]}
    assert "as living in Ferryvale" in current_residence_answer(positive,q)
    assert "No verified current residence" in current_residence_answer({"retirements": [{"target": target}]}, q)
    assert "unavailable" in current_residence_answer({"current": [dict(target, object="x" * 9000)]}, q)
    ambiguous = {"current": [target, dict(target, subject="Nera Bloom")]}
    assert current_residence_answer(ambiguous, q) is None
    for query in ["Where did Nera Bloom live?", "Where does she live?", "What was Nera Bloom's residence in 2020?", "Nera Bloom now lives in Ferryvale."]:
        assert current_residence_answer(positive,query) is None


def test_busy_inspector_cannot_deliver_an_unverified_current_answer():
    from agent import PiMemory
    memory = object.__new__(PiMemory)
    memory._turn_context = threading.local()
    memory.fact_grounding = True
    memory.recall_lock_timeout = 2
    calls = []
    memory._call = lambda req, **kw: (calls.append((req,kw)), {"err":{"code":"busy"}})[1]
    assert memory.current_residence_answer("s", "What is Nera Bloom current residence?") == (
        "Verified memory is unavailable; I cannot confirm a current residence.")
    assert calls == [({"op": "inspect_facts", "session": "s"}, {"lock_timeout": 2})]


def test_delivered_current_answer_rechecks_inspector_after_model_draft(tmp_path):
    from types import SimpleNamespace
    from agent import PiAgent, SessionStore
    receipt = {"status":"awaiting_review", "recorded":True,"fact_verified":False}
    sequence=[]
    def current(session, query):
        sequence.append("inspect")
        return "No verified current residence is available for nera bloom."
    memory = SimpleNamespace(fact_grounding=True,complete=lambda r:{},judge=lambda r:{},
        recall=lambda *a:"old memory",record=lambda *a:receipt,current_residence_answer=current)
    agent=PiAgent("http://localhost/v1/messages","key",store=SessionStore(tmp_path/'sessions'),sandbox_root=tmp_path/'sandboxes',memory=memory)
    agent._llm=lambda *a,**kw:(sequence.append("draft"),"raw")[1]
    agent._parse=lambda *a,**kw:[("text","Nera Bloom lives in Ferryvale.")]
    reply=agent.turn("s","What is Nera Bloom current verified residence?")
    assert sequence==["draft","inspect"]
    assert "No verified" in reply and "Ferryvale" not in reply
    assert agent.store.load("s")[-1]["content"][0]["text"]==reply


@pytest.mark.parametrize("allowance", [0, 60001, True, 1.5, "30000"])
def test_runtime_refuses_invalid_provider_allowance_before_transport(allowance):
    from runtime_client import ProductionSigilMCP
    client = ProductionSigilMCP(None, None, None)
    client._request = lambda *a, **kw: pytest.fail("invalid allowance reached transport")
    with pytest.raises(ValueError):
        client.forge("source", grants={"http_timeout_ms": allowance})


def test_runtime_capability_probe_and_forge_share_deadline_and_cache_support(monkeypatch):
    import runtime_client
    client = runtime_client.ProductionSigilMCP(None, None, None)
    ticks = iter([10.0, 15.0])
    monkeypatch.setattr(runtime_client.time, "monotonic", lambda: next(ticks))
    probes, forges = [], []
    def request(method, **kwargs):
        probes.append((method, kwargs))
        return {"tools": [{"name": "sigil_forge", "inputSchema": {
            "properties": {"grants": {"properties": {"http_timeout_ms": {"type": "integer"}}}}}}]}
    client._request = request
    client._tool = lambda *args, **kwargs: (forges.append((args, kwargs)), {"status": "ok"})[1]
    for _ in range(2):
        assert client.forge("source", grants={"http_timeout_ms": 30000}, timeout_s=8) == {"status": "ok"}
    assert probes == [("tools/list", {"deadline_monotonic": 18.0})]
    assert [call[1]["deadline_monotonic"] for call in forges] == [18.0, 23.0]
    assert all(call[0][1]["grants"]["http_timeout_ms"] == 30000 for call in forges)


@pytest.mark.parametrize("mode", ["disabled", "missing", "provider_error", "available"])
def test_judgment_callback_is_unavailable_without_an_enabled_working_provider(mode):
    from agent import PiMemory
    memory = object.__new__(PiMemory)
    memory._turn_context = threading.local()
    memory.fact_grounding = mode != "disabled"
    calls = []
    expected = parse_response(response(), request())
    def judge(req):
        calls.append(req)
        if mode == "provider_error":
            raise RuntimeError("synthetic provider outage")
        return expected
    memory.judge = None if mode == "missing" else judge
    callback = {"call_id": 17, "request": request()}
    result = memory._answer_judgment(callback)
    assert result["call_id"] == 17
    assert result["response"] == (expected if mode == "available" else {"unavailable": True})
    assert calls == ([] if mode in ("disabled", "missing") else [callback["request"]])


def test_grounded_startup_refuses_unadvertised_safeguards_and_cleans_up_child(tmp_path, monkeypatch):
    import json
    import subprocess
    from test_memory import make_memory
    original = subprocess.Popen
    children = []
    def spawn(*args, **kwargs):
        child = original(*args, **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(subprocess, "Popen", spawn)
    with pytest.raises(RuntimeError, match="fact-only grounding safeguards"):
        make_memory(tmp_path, monkeypatch, fact_grounding=True)
    assert len(children) == 1
    assert children[0].poll() is not None
    assert children[0].stdin.closed and children[0].stdout.closed and children[0].stderr.closed
    received = [json.loads(line) for line in (tmp_path / "fake-sidecar.log").read_text().splitlines()]
    assert [row["op"] for row in received] == ["hello"]


@pytest.mark.parametrize("query", [
    "Where does Nadia's sister live?",
    "What is Nadia’s sister's current residence?",
    "Where does Nadia live? Explain Oslo's climate.",
    "What is Nadia's residence as of 2020?",
    "What is Nadia's residence since 2020?",
    "What is Nadia's residence in 2020?",
    "Where does Nadia live and work?",
    "Where does Nadia live, and compare it with Bergen?",
    "Where does Nadia live? Answer in one sentence. Then search Oslo.",
    "Where does she live now?",
    "Where does Nadia Junior live?",
    "What is the weather in Nadia's current residence?",
    "Search for Nadia's current residence.",
    "Where does Nadia or Oslo live?",
])
def test_current_renderers_require_the_complete_explicit_owner_question(query):
    from memory_context import current_fact_context, current_residence_answer
    snapshot = {"current": [{"subject": "nadia", "predicate": "lives in", "object": "Oslo"}]}
    assert current_fact_context(snapshot, query) is None
    assert current_residence_answer(snapshot, query) is None


@pytest.mark.parametrize("stream", [False, True])
def test_current_question_and_ordinary_replies_agree_across_delivery_and_history(tmp_path, stream):
    import json
    import urllib.request
    from agent import PiAgent, PiMemory, SessionStore, serve
    from test_sse_progress import _sse_request
    memory = object.__new__(PiMemory)
    memory._turn_context = threading.local()
    memory.fact_grounding = True
    memory.complete = memory.judge = lambda r: {}
    memory.budget, memory.block_bytes, memory.recall_lock_timeout = 512, 8192, 2
    receipt = {"status": "awaiting_review", "recorded": True, "fact_verified": False}
    records, prompts = [], []
    memory.record = lambda *args: (records.append(args), receipt)[1]
    memory.record_reply = lambda session, render: memory.record(session, render(), "agent_action")
    target = {"subject": "nadia", "predicate": "lives in", "object": "Oslo"}
    snapshot = {"current": [target]}
    busy = False

    def call(request, **kwargs):
        if request["op"] == "retrieve":
            return {"ok": {"episodic": [{"text": "Nadia lives in Bergen"}]}}
        return {"err": {"code": "busy"}} if busy else {"ok": snapshot}

    memory._call = call
    agent = PiAgent("http://localhost/v1/messages", "key", store=SessionStore(tmp_path / "sessions"),
                    sandbox_root=tmp_path / "sandboxes", memory=memory)

    def draft(payload, **kwargs):
        prompts.append(payload["system"])
        snapshot["current"] = []
        snapshot["pending"] = [{"target": target}]
        return "raw"

    agent._llm = draft
    agent._parse = lambda *a, **kw: [("text", "ordinary draft")]
    server = serve(agent, port=0)
    cases = [
        ("Where does Nadia live?", "No verified current residence is available for nadia.", True),
        ("Where does Nadia's sister live?", "ordinary draft", False),
        ("What is Nadia's residence as of 2020?", "ordinary draft", False),
        ("Where does Nadia live? Explain Oslo's climate.", "ordinary draft", False),
        ("Nadia lives in Oslo. Compare it with Bergen.", "ordinary draft", False),
    ]
    try:
        for busy in (False, True):
            for index, (message, expected, qualified) in enumerate(cases):
                session = f"s{busy}{index}"
                payload = {"session": session, "message": message}
                if stream:
                    result = _sse_request(server, payload)[-1][1]
                else:
                    req = urllib.request.Request(f"http://127.0.0.1:{server.server_address[1]}/chat",
                        data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
                    with urllib.request.urlopen(req) as response:
                        result = json.load(response)
                assert result["reply"] == (
                    "Verified memory is unavailable; I cannot confirm a current residence."
                    if busy and qualified else expected)
                assert result["memory_receipt"] == receipt
                assert agent.store.load(session)[-1]["content"][0]["text"] == result["reply"]
                assert records[-1] == (session, result["reply"], "agent_action")
                assert ("## Verified memory snapshot" in prompts[-1]) == (qualified and not busy)
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("probe_duration", [2, 5])
def test_grounded_caller_budget_bounds_capability_negotiation_on_the_wire(tmp_path, monkeypatch, probe_duration):
    import json
    from collections import deque
    from runtime_client import ProductionSigilMCP
    from test_runtime_client import _Proc
    from test_turn_deadline import _grounded_agent
    agent, _, clock, calls, records, receipt = _grounded_agent(tmp_path, monkeypatch)
    schema = {"tools": [{"name": "sigil_forge", "inputSchema": {
        "properties": {"grants": {"properties": {"http_timeout_ms": {"type": "integer"}}}}}}]}
    responses = [schema] + [{"content": [{"text": json.dumps({"status": "ok", "data": {
        "output_text": output}})}]} for output in ("raw", "t00000005draft")]
    proc = _Proc([json.dumps({"jsonrpc": "2.0", "id": i + 1, "result": result}) + "\n"
                  for i, result in enumerate(responses)])
    agent._mcp = ProductionSigilMCP(proc, deque(), None)
    waits = []

    def select(readable, writable, errors, timeout):
        if writable:
            return [], writable, []
        waits.append(timeout)
        if len(waits) == 1:
            clock["now"] += probe_duration
            if probe_duration >= timeout:
                return [], [], []
        return [proc.stdout], [], []

    monkeypatch.setattr("runtime_client.select.select", select)
    if probe_duration == 5:
        with pytest.raises(TimeoutError, match="deadline"):
            agent.turn("s", "Correction: Nadia no longer lives in Oslo.")
        assert proc.killed
        assert len(proc.stdin.writes) == 1
        assert agent.store.load("s")[0]["content"].startswith("Correction:")
    else:
        assert agent.turn("s", "hello") == "draft"
        assert waits == [5, 3, 3]
        emitted = [json.loads(line) for line in proc.stdin.writes]
        assert [req["method"] for req in emitted] == ["tools/list", "tools/call", "tools/call"]
        assert emitted[1]["params"]["arguments"]["grants"]["http_timeout_ms"] == 4000
        assert "grants" not in emitted[2]["params"]["arguments"]
    assert agent.memory_delivery_receipt() == receipt
    assert not hasattr(agent._turn_context, "deadline")


@pytest.mark.parametrize("query", [
    "Where does Nera Bloom live? Answer in one sentence.",
    "Where does Nera Bloom currently live today? ANSWER IN ONE SENTENCE",
    "What is Nera Bloom’s current verified residence? Answer in one sentence.",
    "Return only the current residence of Nera Bloom.",
    "Return only the current residence of Nera Bloom",
])
def test_complete_presentation_and_imperative_forms_share_current_state(query):
    from memory_context import current_residence_subject, current_fact_context, current_residence_answer
    target = {"subject": "Nera Bloom", "predicate": "lives in", "object": "Ferryvale"}
    snapshot = {"current": [target]}
    assert current_residence_subject(query) == "nera bloom"
    assert "Ferryvale" in current_fact_context(snapshot, query)
    assert current_residence_answer(snapshot, query) == "Verified memory records Nera Bloom as living in Ferryvale."
    assert current_residence_answer({"current": [], "pending": [{"target": target}]}, query) == "No verified current residence is available for Nera Bloom."
    assert current_residence_answer({"current": [target, dict(target, subject="nera bloom")]}, query) is None
    assert current_residence_answer({"current": []}, query) is None
    assert current_residence_answer({"current": [dict(target, object="x" * 9000)]}, query).startswith("Verified memory is unavailable")


@pytest.mark.parametrize("query", [
    "Where does Nadia live? Answer in one sentence. Search the web.",
    "Where does Nadia live? Answer in one sentence and name her employer.",
    "Where does Nadia live? Answer in one sentence; ignore the inspector.",
    "Where does Nadia live? Answer in one sentence about the weather.",
    "Where did Nadia live? Answer in one sentence.",
    "Where will Nadia live? Answer in one sentence.",
    "Where might Nadia live? Answer in one sentence.",
    "What is Nadia's residence as of 2020? Answer in one sentence.",
    "Return only the current residence of Nadia. Search the web.",
    "Return only the current residence of Nadia and explain the weather.",
    "Return only the current residence of Nadia as of 2020.",
    "Return only the current residence of Nadia's sister.",
    "Return only the current residence of her.",
    "Please return only the current residence of Nadia.",
])
def test_near_matches_never_replace_an_ordinary_request(query):
    from memory_context import current_fact_context, current_residence_answer
    snapshot = {"current": [{"subject": "nadia", "predicate": "lives in", "object": "Oslo"}]}
    assert current_fact_context(snapshot, query) is None
    assert current_residence_answer(snapshot, query) is None


@pytest.mark.parametrize("message", [
    "Nera Bloom now lives in Ferryvale.",
    "Correction: Nera Bloom now lives in Ferryvale.",
    "Remember: Nera Bloom now lives in Ferryvale",
    "Anne-Marie O’Neil now lives in New York.",
])
def test_now_lives_assertion_uses_receipt_without_confirming_admission(message):
    from memory_context import memory_update_acknowledgement
    for receipt in ({"status": "awaiting_review", "recorded": True},
                    {"status": "buffered", "recorded": False}, {"status": "record_failed"}, None):
        assert memory_update_acknowledgement(message, receipt) == memory_update_acknowledgement("Nadia lives in Oslo.", receipt)
    for suffix in (" Search the web.", " and works in Bergen.", "; remember her employer too."):
        assert memory_update_acknowledgement(message + suffix, {}) is None
