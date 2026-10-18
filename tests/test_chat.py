import pytest
from fastapi.testclient import TestClient

from app.harness import bedrock, core
from app.main import app

client = TestClient(app)


def test_chat_returns_model_response(monkeypatch):
    monkeypatch.setattr(bedrock, "generate_response", lambda message: "Model reply")
    response = client.post("/chat", json={"message": "こんにちは"})

    assert response.status_code == 200
    assert response.json() == {"response": "Model reply"}


def test_chat_calls_harness(monkeypatch):
    messages = []

    def fake_process_message(message):
        messages.append(message)
        return "Harness result"

    monkeypatch.setattr(core, "process_message", fake_process_message)

    response = client.post("/chat", json={"message": "hello"})

    assert messages == ["hello"]
    assert response.status_code == 200
    assert response.json() == {"response": "Harness result"}


@pytest.mark.parametrize(
    "payload",
    [{}, {"message": ""}, {"message": " \t\n"}, {"message": None},
     {"message": 123}, {"message": True}, {"message": []}, {"message": {}}],
)
def test_chat_rejects_invalid_input(payload, monkeypatch):
    def unexpected_call(message):
        pytest.fail("Invalid input must not reach the harness")

    monkeypatch.setattr(core, "process_message", unexpected_call)

    assert client.post("/chat", json=payload).status_code == 422


def test_chat_rejects_malformed_json():
    response = client.post(
        "/chat", content="{", headers={"Content-Type": "application/json"}
    )

    assert response.status_code == 422


def test_process_message(monkeypatch):
    messages = []

    def fake_generate(message):
        messages.append(message)
        return "Model reply"

    monkeypatch.setattr(bedrock, "generate_response", fake_generate)
    assert core.process_message("hello") == "Model reply"
    assert messages == ["hello"]


def test_bedrock_failure_does_not_stop_api(monkeypatch):
    def fail(message):
        raise bedrock.BedrockError("secret upstream details")

    monkeypatch.setattr(bedrock, "generate_response", fail)
    response = client.post("/chat", json={"message": "hello"})
    assert response.status_code == 502
    assert response.json() == {
        "detail": "Model service unavailable. Please try again."
    }
    monkeypatch.setattr(bedrock, "generate_response", lambda message: "Recovered")
    assert client.post("/chat", json={"message": "hello"}).json() == {
        "response": "Recovered"
    }
