import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from app.config import ConfigurationError
from app.harness import bedrock
from app.lambda_handler import handler
from app.main import app


@pytest.fixture
def mock_mode(monkeypatch):
    monkeypatch.setenv("HARNESS_BACKEND", "mock")
    monkeypatch.delenv("BEDROCK_MODEL_ID", raising=False)
    aws = Mock(side_effect=AssertionError("Mock must not contact AWS"))
    monkeypatch.setattr(bedrock.boto3, "client", aws)
    return aws


def test_local_mock(mock_mode):
    with TestClient(app) as client:
        assert client.post("/chat", json={"message": "hello"}).json() == {"response": "Mock: hello"}
        assert client.post("/chat", json={"message": " "}).status_code == 422
    mock_mode.assert_not_called()


@pytest.mark.parametrize("message,status", [("hello", 200), ("", 422), (123, 422)])
def test_lambda_event(mock_mode, message, status):
    event = json.loads((Path(__file__).parents[1] / "events/chat.json").read_text())
    event["body"] = json.dumps({"message": message})
    response = handler(event, Mock())
    assert response["statusCode"] == status
    if status == 200:
        assert json.loads(response["body"]) == {"response": "Mock: hello"}
    mock_mode.assert_not_called()


def test_lambda_bedrock_failure(monkeypatch):
    monkeypatch.setenv("BEDROCK_MODEL_ID", "test.model:0")
    monkeypatch.setattr(bedrock, "generate_response", Mock(side_effect=bedrock.BedrockError("private details")))
    event = json.loads((Path(__file__).parents[1] / "events/chat.json").read_text())
    response = handler(event, Mock())
    assert response["statusCode"] == 502
    assert "private details" not in response["body"]


def test_invalid_backend(monkeypatch):
    monkeypatch.setenv("HARNESS_BACKEND", "typo")
    with pytest.raises(ConfigurationError):
        with TestClient(app):
            pass
