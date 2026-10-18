import logging
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from app.config import ConfigurationError, get_bedrock_model_id
from app.harness import bedrock
from app.main import app


@pytest.mark.parametrize("value", [None, "", " \t", "model\nforged", "secret=value"])
def test_invalid_configuration(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("BEDROCK_MODEL_ID", raising=False)
    else:
        monkeypatch.setenv("BEDROCK_MODEL_ID", value)
    with pytest.raises(ConfigurationError):
        get_bedrock_model_id()
    with pytest.raises(ConfigurationError):
        with TestClient(app):
            pass


def test_startup_and_request_use_environment(monkeypatch, caplog):
    model_id = "global.amazon.nova-2-lite-v1:0"
    monkeypatch.setenv("BEDROCK_MODEL_ID", model_id)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "private-secret")
    runtime = Mock()
    runtime.converse.return_value = {"output": {"message": {"content": [{"text": "reply"}]}}}
    monkeypatch.setattr(bedrock.boto3, "client", Mock(return_value=runtime))
    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        with TestClient(app) as client:
            assert client.post("/chat", json={"message": "hello"}).json() == {"response": "reply"}
    assert f"Bedrock model ID: {model_id}" in caplog.text
    assert runtime.converse.call_args.kwargs["modelId"] == model_id
    assert "private-secret" not in caplog.text
    # Reading settings after import must not retain a stale model ID.
    monkeypatch.setenv("BEDROCK_MODEL_ID", "another.test-model:0")
    assert get_bedrock_model_id() == "another.test-model:0"


def test_credentials_are_not_logged_as_model(monkeypatch, caplog):
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "private-secret")
    monkeypatch.setenv("BEDROCK_MODEL_ID", "private-secret")
    with pytest.raises(ConfigurationError):
        with TestClient(app):
            pass
    assert "private-secret" not in caplog.text


def test_missing_model_does_not_call_aws(monkeypatch):
    monkeypatch.delenv("BEDROCK_MODEL_ID", raising=False)
    factory = Mock()
    monkeypatch.setattr(bedrock.boto3, "client", factory)
    with pytest.raises(bedrock.BedrockError):
        bedrock.generate_response("hello")
    factory.assert_not_called()
