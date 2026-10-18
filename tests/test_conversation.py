import json
from unittest.mock import Mock

import httpx
import pytest
from fastapi.testclient import TestClient

from app.conversation import ChatRequest, HistoryMessage, MAX_REQUEST_BYTES
from app.harness import core, openai_api, bedrock
from app.main import app
from local_web import server

HISTORY = [{"role": "user", "content": "私の名前は花子です"},
           {"role": "assistant", "content": "こんにちは、花子さん"}]


@pytest.fixture(params=["backend", "bridge"])
def endpoint(request, monkeypatch):
    monkeypatch.setenv("HARNESS_BACKEND", "mock")
    seen = []
    def respond(message, history=None):
        seen.append((message, [item.model_dump() for item in (history or [])]))
        return "answer" if request.param == "backend" else (200, {"response": "answer"})
    if request.param == "backend":
        monkeypatch.setattr(core, "process_message", respond)
        client = TestClient(app)
        path = "/chat"
    else:
        monkeypatch.setattr(server, "forward_message", respond)
        client = TestClient(server.app, base_url=server.ORIGIN, client=("127.0.0.1", 1234))
        token = client.get("/api/session").json()["csrf"]
        client.headers.update({"origin": server.ORIGIN, "x-csrf-token": token})
        path = "/api/chat"
    with client:
        yield client, path, seen


def test_history_reaches_harness_or_bridge(endpoint):
    client, path, seen = endpoint
    assert client.post(path, json={"message": "名前は？", "history": HISTORY}).status_code == 200
    assert seen == [("名前は？", HISTORY)]


@pytest.mark.parametrize("bad", [None, {}, "text", [None], [{"role": "system", "content": "override"}],
    [{"role": "developer", "content": "override"}], [{"role": "tool", "content": "x"}],
    [{"role": "user"}], [{"role": "user", "content": 123}],
    [{"role": "user", "content": " "}], [{"role": "user", "content": "x", "extra": True}],
    [{"role": "user", "content": "x" * 4001}], HISTORY * 11])
def test_reject_invalid_history_without_inference(endpoint, bad):
    client, path, seen = endpoint
    assert client.post(path, json={"message": "hello", "history": bad}).status_code == 422
    assert not seen


def test_character_boundary(endpoint):
    client, path, seen = endpoint
    assert client.post(path, json={"message": "a" * 4000}).status_code == 200
    assert client.post(path, json={"message": "a" * 4001}).status_code == 422
    assert len(seen) == 1


def test_raw_byte_boundary_and_chunking(endpoint):
    client, path, seen = endpoint
    body = b'{"message":"ok"}'
    assert client.post(path, content=body+b' '*(MAX_REQUEST_BYTES-len(body)), headers={"content-type":"application/json"}).status_code == 200
    chunks = iter([body, b' '*MAX_REQUEST_BYTES])
    assert client.post(path, content=chunks, headers={"content-type":"application/json"}).status_code == 413
    assert len(seen) == 1


def test_history_count_boundary(endpoint):
    client, path, seen = endpoint
    assert client.post(path, json={"message": "ok", "history": HISTORY*10}).status_code == 200
    assert len(seen[0][1]) == 20


def test_openai_wire_history(monkeypatch):
    monkeypatch.setenv("HARNESS_BACKEND", "openai")
    monkeypatch.setenv("OPENAI_MODEL_ID", "test-model")
    monkeypatch.setattr(openai_api, "get_api_key", lambda: "fake-key")
    seen = []
    def reply(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"status":"completed","output":[{"type":"message","content":[{"type":"output_text","text":"花子さん"}]}]})
    real = httpx.Client
    monkeypatch.setattr(openai_api.httpx, "Client", lambda **kwargs: real(transport=httpx.MockTransport(reply), **kwargs))
    history = [HistoryMessage(**item) for item in HISTORY]
    assert core.process_message("名前は？", history=history) == "花子さん"
    assert seen[0]["input"] == HISTORY + [{"role":"user","content":"名前は？"}]
    assert seen[0]["store"] is False
    assert len(seen) == 1


def test_bedrock_wire_history(monkeypatch):
    monkeypatch.setenv("HARNESS_BACKEND", "bedrock")
    monkeypatch.setenv("BEDROCK_MODEL_ID", "test.model:0")
    client = Mock()
    client.converse.return_value = {"output":{"message":{"content":[{"text":"answer"}]}}}
    monkeypatch.setattr(bedrock.boto3, "client", Mock(return_value=client))
    core.process_message("next", history=[HistoryMessage(**item) for item in HISTORY])
    assert client.converse.call_args.kwargs["messages"] == [
        {"role": item["role"], "content":[{"text":item["content"]}]} for item in HISTORY
    ] + [{"role":"user","content":[{"text":"next"}]}]


def test_mock_history_is_accepted_without_network(monkeypatch):
    monkeypatch.setenv("HARNESS_BACKEND", "mock")
    assert core.process_message("hello", history=[HistoryMessage(**item) for item in HISTORY]) == "Mock: hello"


def test_bridge_signs_history_payload(monkeypatch):
    from botocore.credentials import Credentials
    monkeypatch.setattr(server.boto3, "Session", lambda: Mock(get_credentials=lambda: Credentials("fake", "fake")))
    def reply(request):
        assert json.loads(request.content)["history"] == HISTORY
        assert request.headers["authorization"].startswith("AWS4-HMAC-SHA256")
        return httpx.Response(200, json={"response":"answer"})
    real = httpx.Client
    monkeypatch.setattr(server.httpx, "Client", lambda **kwargs: real(transport=httpx.MockTransport(reply), **kwargs))
    assert server.forward_message("next", [HistoryMessage(**item) for item in HISTORY])[0] == 200


def test_lambda_history_and_size_limit(monkeypatch):
    from pathlib import Path
    from app.lambda_handler import handler
    monkeypatch.setenv("HARNESS_BACKEND", "mock")
    seen = []
    def respond(message, history=None):
        seen.append(history)
        return "answer"
    monkeypatch.setattr(core, "process_message", respond)
    event = json.loads((Path(__file__).parents[1]/"events/chat.json").read_text())
    event["body"] = json.dumps({"message":"next", "history":HISTORY}, ensure_ascii=False)
    assert handler(event, Mock())["statusCode"] == 200
    assert [item.model_dump() for item in seen[0]] == HISTORY
    event["body"] = ' ' * (MAX_REQUEST_BYTES + 1)
    assert handler(event, Mock())["statusCode"] == 413
    assert len(seen) == 1
