import json
from unittest.mock import Mock

import httpx
import pytest
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient

from app.config import ConfigurationError
from app.harness import core, openai_api
from app.main import app

KEY = "fake-test-key-never-valid"
SUCCESS = {"status": "completed", "output": [
    {"type": "reasoning"},
    {"type": "message", "content": [{"type": "output_text", "text": "Hello"}]},
]}


@pytest.fixture
def configure(monkeypatch):
    monkeypatch.setenv("HARNESS_BACKEND", "openai")
    monkeypatch.setenv("OPENAI_MODEL_ID", "test-model")
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    monkeypatch.delenv("AWS_LAMBDA_FUNCTION_NAME", raising=False)
    monkeypatch.delenv("OPENAI_MAX_OUTPUT_TOKENS", raising=False)
    monkeypatch.delenv("OPENAI_TIMEOUT_SECONDS", raising=False)
    original = httpx.Client
    def install(handler):
        def client(**kwargs):
            assert kwargs["follow_redirects"] is False
            assert kwargs["trust_env"] is False
            return original(transport=httpx.MockTransport(handler), **kwargs)
        monkeypatch.setattr(openai_api.httpx, "Client", client)
    return install


def test_responses_request(configure):
    seen = []
    def handler(request):
        seen.append(request)
        assert str(request.url) == openai_api.ENDPOINT
        assert request.headers["authorization"] == f"Bearer {KEY}"
        assert json.loads(request.content) == {
            "model": "test-model", "input": "hello", "max_output_tokens": 512, "store": False
        }
        return httpx.Response(200, json=SUCCESS)
    configure(handler)
    assert core.process_message("hello") == "Hello"
    assert len(seen) == 1


@pytest.mark.parametrize("status,code,category", [
    (401,"invalid_api_key","authentication"), (403,"denied","authentication"),
    (429,"rate_limit_exceeded","rate_limit"), (429,"insufficient_quota","billing_or_quota"),
    (429,"project_spend_limit_exceeded","billing_or_quota"),
    (402,"billing_not_active","billing_or_quota"), (500,"server_error","upstream_http"),
    (302,"redirect","upstream_http"),
])
def test_api_errors_are_generic_and_redacted(configure, status, code, category, caplog):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(status,json={"error":{"code":code,"message":KEY}})
    configure(handler)
    with TestClient(app) as client:
        response=client.post("/chat",json={"message":"hello"})
    assert response.status_code == 502
    assert response.json()=={"detail":"Model service unavailable. Please try again."}
    assert category in caplog.text
    assert KEY not in caplog.text + response.text
    assert len(calls)==1


@pytest.mark.parametrize("error,category", [(httpx.ReadTimeout(KEY),"timeout"),(httpx.ConnectError(KEY),"network")])
def test_transport_errors(configure,error,category,caplog):
    def handler(request): raise error
    configure(handler)
    with pytest.raises(openai_api.OpenAIError,match=category):
        core.process_message("hello")
    assert KEY not in caplog.text


@pytest.mark.parametrize("payload", [{}, {"status":"incomplete","output":[]}, {"status":"completed","output":[]}])
def test_bad_response(configure,payload):
    configure(lambda request:httpx.Response(200,json=payload))
    with pytest.raises(openai_api.OpenAIError): core.process_message("hello")


def test_missing_local_key(configure,monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY")
    configure(lambda request:pytest.fail("No call without key"))
    with pytest.raises(openai_api.OpenAIError,match="credentials_unavailable"):
        core.process_message("hello")


def test_lambda_secret_overrides_environment(configure,monkeypatch):
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME","test")
    monkeypatch.setenv("OPENAI_SECRET_ID","test-secret-arn")
    client=Mock()
    client.get_secret_value.return_value={"SecretString":json.dumps({"api_key":"fake-secret-key"})}
    monkeypatch.setattr(openai_api.boto3,"client",Mock(return_value=client))
    assert openai_api.get_api_key()=="fake-secret-key"
    client.get_secret_value.assert_called_once_with(SecretId="test-secret-arn", VersionStage="AWSCURRENT")


def test_lambda_never_falls_back_to_env_key(configure,monkeypatch):
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME","test")
    monkeypatch.delenv("OPENAI_SECRET_ID",raising=False)
    with pytest.raises(openai_api.OpenAIError,match="secret_configuration"):
        openai_api.get_api_key()


def test_secret_access_error_is_redacted(configure,monkeypatch,caplog):
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME","test")
    monkeypatch.setenv("OPENAI_SECRET_ID","test-secret")
    client=Mock()
    client.get_secret_value.side_effect=ClientError({"Error":{"Code":"AccessDeniedException","Message":KEY}},"GetSecretValue")
    monkeypatch.setattr(openai_api.boto3,"client",Mock(return_value=client))
    with pytest.raises(openai_api.OpenAIError,match="secret_unavailable"):
        core.process_message("hello")
    assert KEY not in caplog.text


@pytest.mark.parametrize("name,value", [("OPENAI_MODEL_ID",""),("OPENAI_MAX_OUTPUT_TOKENS","0"),("OPENAI_MAX_OUTPUT_TOKENS","abc"),("OPENAI_TIMEOUT_SECONDS","999")])
def test_invalid_settings(configure,monkeypatch,name,value):
    monkeypatch.setenv(name,value)
    with pytest.raises(ConfigurationError): openai_api.get_settings()


@pytest.mark.parametrize("secret", ["not-json", "{}", '{"api_key":""}', '[]'])
def test_invalid_secret(configure,monkeypatch,secret):
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME","test")
    monkeypatch.setenv("OPENAI_SECRET_ID","test-secret")
    client=Mock()
    client.get_secret_value.return_value={"SecretString":secret}
    monkeypatch.setattr(openai_api.boto3,"client",Mock(return_value=client))
    with pytest.raises(openai_api.OpenAIError): openai_api.get_api_key()


def test_response_limit(configure):
    configure(lambda request:httpx.Response(200,content=b"x"*(1024*1024+1)))
    with pytest.raises(openai_api.OpenAIError,match="response_too_large"):
        core.process_message("hello")
