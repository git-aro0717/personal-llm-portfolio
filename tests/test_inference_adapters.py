from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError, ReadTimeoutError
from fastapi.testclient import TestClient

from app.config import get_harness_backend
from app.harness import bedrock, core
from app.main import app


def test_default_is_offline_mock(monkeypatch):
    monkeypatch.delenv("HARNESS_BACKEND", raising=False)
    monkeypatch.delenv("BEDROCK_MODEL_ID", raising=False)
    factory = Mock(side_effect=AssertionError("Mock must not call AWS"))
    monkeypatch.setattr(bedrock.boto3, "client", factory)
    assert get_harness_backend() == "mock"
    assert core.process_message("hello") == "Mock: hello"
    factory.assert_not_called()


@pytest.fixture
def runtime(monkeypatch):
    monkeypatch.setenv("HARNESS_BACKEND", "bedrock")
    monkeypatch.setenv("BEDROCK_MODEL_ID", "global.amazon.nova-2-lite-v1:0")
    runtime = Mock()
    factory = Mock(return_value=runtime)
    monkeypatch.setattr(bedrock.boto3, "client", factory)
    return runtime, factory


def test_bedrock_success_through_backend(runtime):
    runtime[0].converse.return_value = {
        "output": {"message": {"content": [{"text": "Test model answer"}]}}
    }
    with TestClient(app) as client:
        response = client.post("/chat", json={"message": "hello"})
    assert response.status_code == 200
    assert response.json() == {"response": "Test model answer"}
    assert runtime[0].converse.call_args.kwargs["modelId"] == "global.amazon.nova-2-lite-v1:0"
    config = runtime[1].call_args.kwargs["config"]
    assert config.retries["total_max_attempts"] == 1
    assert config.connect_timeout + config.read_timeout < 150


@pytest.mark.parametrize("code", [
    "AccessDeniedException", "ThrottlingException", "ServiceQuotaExceededException",
    "ModelTimeoutException",
])
def test_bedrock_service_errors_are_generic_502(runtime, code, caplog):
    runtime[0].converse.side_effect = ClientError(
        {"Error": {"Code": code, "Message": "upstream diagnostic"}}, "Converse"
    )
    with TestClient(app) as client:
        response = client.post("/chat", json={"message": "hello"})
        assert response.status_code == 502
        assert response.json() == {"detail": "Model service unavailable. Please try again."}
        assert code in caplog.text
        assert "upstream diagnostic" not in response.text
        runtime[0].converse.side_effect = None
        runtime[0].converse.return_value = {"output": {"message": {"content": [{"text": "recovered"}]}}}
        assert client.post("/chat", json={"message": "hello"}).json() == {"response": "recovered"}


def test_transport_timeout_is_generic_502(runtime):
    runtime[0].converse.side_effect = ReadTimeoutError(endpoint_url="https://example.invalid")
    with TestClient(app) as client:
        response = client.post("/chat", json={"message": "hello"})
    assert response.status_code == 502
    assert "example.invalid" not in response.text


def test_switch_back_to_mock_without_network(runtime, monkeypatch):
    monkeypatch.setenv("HARNESS_BACKEND", "mock")
    assert core.process_message("hello") == "Mock: hello"
    runtime[1].assert_not_called()
