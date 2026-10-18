import json
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest
from botocore.exceptions import ClientError, ReadTimeoutError

from app.harness import openai_api
from app.lambda_handler import handler


@pytest.fixture
def setup_lambda(monkeypatch):
    monkeypatch.setenv("HARNESS_BACKEND", "openai")
    monkeypatch.setenv("OPENAI_MODEL_ID", "test-model")
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "test-function")
    monkeypatch.setenv("OPENAI_SECRET_ID", "dedicated-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-be-used")
    monkeypatch.delenv("OPENAI_MAX_OUTPUT_TOKENS", raising=False)
    monkeypatch.delenv("OPENAI_TIMEOUT_SECONDS", raising=False)
    secret = Mock()
    monkeypatch.setattr(openai_api.boto3, "client", Mock(return_value=secret))
    return secret, json.loads((Path(__file__).parents[1] / "events/chat.json").read_text())


@pytest.mark.parametrize("error", [
    ClientError({"Error": {"Code": "AccessDeniedException", "Message": "private-secret"}}, "GetSecretValue"),
    ClientError({"Error": {"Code": "ResourceNotFoundException", "Message": "private-secret"}}, "GetSecretValue"),
    ClientError({"Error": {"Code": "DecryptionFailure", "Message": "private-secret"}}, "GetSecretValue"),
    ReadTimeoutError(endpoint_url="https://example.invalid"),
])
def test_secret_failure_stops_before_openai(setup_lambda, monkeypatch, error, caplog):
    secret, event = setup_lambda
    secret.get_secret_value.side_effect = error
    monkeypatch.setattr(openai_api.httpx, "Client", Mock(side_effect=AssertionError("No HTTP after secret failure")))
    response = handler(event, Mock())
    assert response["statusCode"] == 502
    assert json.loads(response["body"]) == {"detail": "Model service unavailable. Please try again."}
    assert "private-secret" not in response["body"] + caplog.text
    assert "must-not-be-used" not in caplog.text


def test_lambda_secret_to_responses(setup_lambda, monkeypatch):
    secret, event = setup_lambda
    secret.get_secret_value.return_value = {"SecretString": '{"api_key":"fake-dedicated-key"}'}
    real_client = httpx.Client
    def respond(request):
        assert request.headers["authorization"] == "Bearer fake-dedicated-key"
        return httpx.Response(200, json={"status":"completed", "output":[
            {"type":"message", "content":[{"type":"output_text", "text":"Test answer"}]}
        ]})
    monkeypatch.setattr(openai_api.httpx, "Client", lambda **kwargs: real_client(transport=httpx.MockTransport(respond), **kwargs))
    response = handler(event, Mock())
    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == {"response":"Test answer"}
    secret.get_secret_value.assert_called_once_with(SecretId="dedicated-secret", VersionStage="AWSCURRENT")


@pytest.mark.parametrize('scenario,status,requests',[
    ('success',200,3),('original_absent',502,2),('attachment_absent',502,1),
    ('unselected',422,0),('forbidden_store',403,0),('not_ready',502,1),
])
def test_lambda_file_search_event(setup_lambda,monkeypatch,scenario,status,requests,caplog):
    secret,event=setup_lambda
    secret.get_secret_value.return_value={'SecretString':'{"api_key":"fake-search-key"}'}
    monkeypatch.setenv('OPENAI_ALLOWED_VECTOR_STORE_IDS','vs_allowed')
    # Lambda must not use relay SQLite, even if a relay-only setting leaks in.
    monkeypatch.setenv('PERSONAL_LLM_DOCUMENT_DB','unusable-local-db-path')
    payload={'message':'fictional question','history':[{'role':'user','content':'previous fictional turn'}],
             'vector_store_id':'vs_other' if scenario=='forbidden_store' else 'vs_allowed','document_ids':['file-test']}
    if scenario=='unselected':payload.pop('document_ids')
    event['body']=json.dumps(payload)
    real=httpx.Client;seen=[]
    def respond(request):
        seen.append((request.method,request.url.path))
        assert request.url.host=='api.openai.com' and request.url.scheme=='https'
        assert request.headers['authorization']=='Bearer fake-search-key'
        if request.url.path=='/v1/vector_stores/vs_allowed/files/file-test':
            return httpx.Response(404 if scenario=='attachment_absent' else 200,
                                  json={'status':'in_progress' if scenario=='not_ready' else 'completed',
                                        'attributes':{'personal_llm_document_id':'file-test'}})
        if request.url.path=='/v1/files/file-test':
            return httpx.Response(404 if scenario=='original_absent' else 200,json={'id':'file-test'})
        assert request.method=='POST' and request.url.path=='/v1/responses'
        data=json.loads(request.content)
        assert data['model']=='test-model'
        assert data['input']==payload['history']+[{'role':'user','content':payload['message']}]
        assert data['tools']==[{'type':'file_search','vector_store_ids':['vs_allowed'],'max_num_results':5,
                               'filters':{'type':'in','key':'personal_llm_document_id','value':['file-test']}}]
        return httpx.Response(200,json={'status':'completed','output':[{'type':'message','content':[
            {'type':'output_text','text':'fictional answer','annotations':[
                {'type':'file_citation','file_id':'file-test','filename':'fiction.txt'}]}]}]})
    monkeypatch.setattr(openai_api.httpx,'Client',lambda **kwargs:real(transport=httpx.MockTransport(respond),**kwargs))
    response=handler(event,Mock())
    assert response['statusCode']==status and len(seen)==requests
    if scenario=='success':
        assert seen==[('GET','/v1/vector_stores/vs_allowed/files/file-test'),('GET','/v1/files/file-test'),('POST','/v1/responses')]
        assert json.loads(response['body'])=={'response':'fictional answer','sources':[{'file_id':'file-test','filename':'fiction.txt'}]}
    else:assert not any(method=='POST' for method,_ in seen)
    if requests:secret.get_secret_value.assert_called_once_with(SecretId='dedicated-secret',VersionStage='AWSCURRENT')
    else:secret.get_secret_value.assert_not_called()
    assert 'fake-search-key' not in response['body']+caplog.text
