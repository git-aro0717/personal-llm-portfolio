from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError, NoCredentialsError, ReadTimeoutError

from app.harness import bedrock


@pytest.fixture
def runtime(monkeypatch):
    monkeypatch.setenv("BEDROCK_MODEL_ID", "global.amazon.nova-2-lite-v1:0")
    client = Mock()
    factory = Mock(return_value=client)
    monkeypatch.setattr(bedrock.boto3, "client", factory)
    return client, factory


def test_converse_request_and_text_extraction(runtime):
    client, factory = runtime
    client.converse.return_value = {
        "output": {"message": {"content": [
            {"reasoningContent": {}}, {"text": "Hello"}, {"text": " world"}
        ]}}
    }
    assert bedrock.generate_response("こんにちは") == "Hello world"
    assert factory.call_args.args == ("bedrock-runtime",)
    assert factory.call_args.kwargs["region_name"] == "ap-southeast-2"
    assert set(factory.call_args.kwargs) == {"region_name", "config"}
    client.converse.assert_called_once_with(
        modelId="global.amazon.nova-2-lite-v1:0",
        messages=[{"role": "user", "content": [{"text": "こんにちは"}]}],
    )


@pytest.mark.parametrize("error", [
    NoCredentialsError(),
    ReadTimeoutError(endpoint_url="https://example.invalid"),
    ClientError({"Error": {"Code": "AccessDeniedException", "Message": "Denied"}}, "Converse"),
])
def test_sdk_errors_are_wrapped(runtime, error, caplog):
    runtime[0].converse.side_effect = error
    with pytest.raises(bedrock.BedrockError):
        bedrock.generate_response("hello")
    assert type(error).__name__ in caplog.text
    assert str(error) in caplog.text
    assert caplog.records[0].exc_info is None


def test_client_creation_failure(runtime):
    runtime[1].side_effect = NoCredentialsError()
    with pytest.raises(bedrock.BedrockError):
        bedrock.generate_response("hello")


@pytest.mark.parametrize("result", [
    {}, None, {"output": {"message": {"content": []}}},
    {"output": {"message": {"content": [{"text": " "}]}}},
    {"output": {"message": {"content": [{"text": None}]}}},
])
def test_invalid_responses_are_wrapped(runtime, result):
    runtime[0].converse.return_value = result
    with pytest.raises(bedrock.BedrockError):
        bedrock.generate_response("hello")


def test_model_override(runtime, monkeypatch):
    monkeypatch.setenv("BEDROCK_MODEL_ID", "global.openai.gpt-5.6-terra")
    runtime[0].converse.return_value = {
        "output": {"message": {"content": [{"text": "reply"}]}}
    }
    assert bedrock.generate_response("hello") == "reply"
    assert runtime[0].converse.call_args.kwargs["modelId"] == "global.openai.gpt-5.6-terra"


def test_logs_redact_credentials(runtime, monkeypatch, caplog):
    from botocore.credentials import Credentials

    access_key = "AKIA" + "A" * 16
    secret_key = "private-secret-value"
    token = "private-session-token"
    runtime[0]._request_signer._credentials = Credentials(access_key, secret_key, token)
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "private-bearer-token")
    runtime[0].converse.side_effect = ClientError({"Error": {
        "Code": "AccessDeniedException",
        "Message": f"Denied {access_key} {secret_key} {token} private-bearer-token "
                   "SecretAccessKey=unknown-secret X-Amz-Signature=unknown-signature\nforged",
    }}, "Converse")
    with pytest.raises(bedrock.BedrockError):
        bedrock.generate_response("hello")
    assert "ClientError" in caplog.text
    assert "AccessDeniedException" in caplog.text
    for value in (access_key, secret_key, token, "private-bearer-token",
                  "unknown-secret", "unknown-signature"):
        assert value not in caplog.text
    assert "[REDACTED]" in caplog.text
    assert "\n" not in caplog.records[0].getMessage()
    assert caplog.records[0].exc_info is None
