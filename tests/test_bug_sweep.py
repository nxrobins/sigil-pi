"""M2 bug sweep — adversarial and operational edges beyond the happy path."""
import hashlib
import json

from conftest import decode_escaped


def test_message_may_contain_pipes(chat):
    """Only the FIRST '|' splits; the message keeps the rest verbatim."""
    chat.mock.replies = ["ok"]
    status, _ = chat.post("/chat", "s1|a|b|c")
    assert status == 200
    assert chat.mock.requests[0].body["messages"] == [
        {"role": "user", "content": "a|b|c"},
    ]


def test_unicode_session_id(chat):
    """kv keys are hashed bytes — any session id works, and stays isolated."""
    chat.mock.replies = ["r"]
    assert chat.post("/chat", "sesh-😀|hi")[0] == 200
    assert chat.post("/chat", "sesh-😀|more")[0] == 200
    assert len(chat.mock.requests[1].body["messages"]) == 3


def test_empty_body_is_400(chat):
    status, _ = chat.post("/chat", "")
    assert status == 400
    assert chat.mock.requests == []


def test_large_message_survives(chat):
    """~200 KB message: escape (6x expansion bound), payload assembly, and
    history write all fit the fuel and kv caps."""
    chat.mock.replies = ["got it"]
    big = ("x" * 1000 + '"quote" and \\ ') * 200  # ~203 KB with escapes
    status, body = chat.post("/chat", "s1|" + big)
    assert status == 200
    assert decode_escaped(body) == "got it"
    assert chat.mock.requests[0].body["messages"][0]["content"] == big


def test_missing_cfg_key_is_500_not_404(chat):
    """Operator error (unseeded cfg) must read as a server fault, not a
    client 404. kv reads hit the disk per request, so deleting the backing
    file after boot simulates the misconfiguration."""
    (chat.cfg_dir / (hashlib.sha256(b"ao").hexdigest() + ".kv")).unlink()
    chat.mock.replies = ["r"]
    status, _ = chat.post("/chat", "s1|hi")
    assert status == 500


def test_upstream_http_status_propagates(chat):
    """A dead upstream surfaces as 502 (transport), not a fake success."""
    chat.mock.replies = ["r"]
    # Point cfg:url at a closed port.
    (chat.cfg_dir / (hashlib.sha256(b"url").hexdigest() + ".kv")).write_bytes(
        b"http://127.0.0.1:9/v1/messages")
    status, _ = chat.post("/chat", "s1|hi")
    assert status == 502


def test_twenty_turn_conversation(chat):
    """History replay stays byte-exact over a long session."""
    chat.mock.replies = [f"reply {i}" for i in range(20)]
    for i in range(20):
        status, _ = chat.post("/chat", f"s1|question {i}")
        assert status == 200
    last = chat.mock.requests[-1].body["messages"]
    assert len(last) == 39  # 20 user + 19 assistant
    assert last[0] == {"role": "user", "content": "question 0"}
    assert last[37] == {"role": "assistant", "content": "reply 18"}
    assert last[38] == {"role": "user", "content": "question 19"}
