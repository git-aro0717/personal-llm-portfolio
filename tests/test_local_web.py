import httpx
import pytest
from fastapi.testclient import TestClient
from local_web import server


@pytest.fixture
def client():
    with TestClient(server.app, base_url=server.ORIGIN, client=("127.0.0.1", 12345)) as client:
        token = client.get("/api/session").json()["csrf"]
        client.headers.update({"origin": server.ORIGIN, "x-csrf-token": token})
        yield client


def test_forwarding(client, monkeypatch):
    calls = []
    def forward(message):
        calls.append(message)
        return 200, {"response": "Mock: hello"}
    monkeypatch.setattr(server, "forward_message", forward)
    assert client.post("/api/chat", json={"message": "hello", "url": "http://evil"}).json() == {"response": "Mock: hello"}
    assert calls == ["hello"]


@pytest.mark.parametrize("header,value", [("host", "evil.example"), ("origin", "http://evil.example"), ("x-csrf-token", "wrong")])
def test_security_headers(client, header, value):
    assert client.post("/api/chat", json={"message":"hello"}, headers={header:value}).status_code == 403


def test_missing_origin_and_cookie(client):
    del client.headers["origin"]
    assert client.post("/api/chat", json={"message":"hello"}).status_code == 403
    client.headers["origin"] = server.ORIGIN
    client.cookies.clear()
    assert client.post("/api/chat", json={"message":"hello"}).status_code == 403


@pytest.mark.parametrize("body", ["{", "{}", '{"message":""}', '{"message":123}'])
def test_invalid_input(client, body):
    assert client.post("/api/chat", content=body, headers={"content-type":"application/json"}).status_code == 422


def test_size_limit(client):
    assert client.post("/api/chat", json={"message":"x"*17000}).status_code == 413


@pytest.mark.parametrize("status", [422,403,502])
def test_upstream_status(client, monkeypatch, status):
    monkeypatch.setattr(server, "forward_message", lambda message: (status,{"detail":"error"}))
    assert client.post("/api/chat", json={"message":"hello"}).status_code == status


@pytest.mark.parametrize("error,status", [(httpx.ConnectError("private detail"),503),(httpx.ReadTimeout("private detail"),504)])
def test_transport_errors(client, monkeypatch, error, status):
    def fail(message): raise error
    monkeypatch.setattr(server,"forward_message",fail)
    response=client.post("/api/chat",json={"message":"hello"})
    assert response.status_code==status
    assert "private detail" not in response.text


def test_fixed_target_sigv4_and_redirect_rejection(monkeypatch):
    from botocore.credentials import Credentials
    from unittest.mock import Mock
    credentials = Credentials("fake-access", "fake-secret", "fake-token")
    monkeypatch.setattr(server.boto3, "Session", lambda: Mock(get_credentials=lambda: credentials))
    seen = []
    def respond(request):
        seen.append(request)
        return httpx.Response(302, headers={"location":"http://evil.example"})
    real_client = httpx.Client
    def test_client(**kwargs):
        assert kwargs["follow_redirects"] is False
        assert kwargs["trust_env"] is False
        return real_client(transport=httpx.MockTransport(respond), **kwargs)
    monkeypatch.setattr(server.httpx,"Client",test_client)
    assert server.forward_message("hello")[0] == 502
    assert len(seen) == 1
    assert str(seen[0].url) == server.TARGET
    assert seen[0].headers["authorization"].startswith("AWS4-HMAC-SHA256")
    assert "/ap-southeast-2/lambda/aws4_request" in seen[0].headers["authorization"]
    assert seen[0].headers["x-amz-security-token"] == "fake-token"
