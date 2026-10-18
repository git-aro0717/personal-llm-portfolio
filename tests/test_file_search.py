import json
from unittest.mock import Mock
import httpx
import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.conversation import HistoryMessage
from app.file_search import StoreForbidden, extract_sources
from app.harness import core, openai_api
from local_web import server


@pytest.fixture
def search(monkeypatch):
    monkeypatch.setenv("HARNESS_BACKEND", "openai")
    monkeypatch.setenv("OPENAI_MODEL_ID", "test-model")
    monkeypatch.setenv("OPENAI_ALLOWED_VECTOR_STORE_IDS", "vs_allowed,vs_second")
    monkeypatch.setattr(openai_api, "get_api_key", lambda: "fake-key")
    seen = []
    annotation = {"type":"file_citation", "file_id":"file-abc", "filename":"../docs/guide.txt", "index":0}
    result = {"status":"completed", "output":[{"type":"file_search_call","status":"completed"},
        {"type":"message","content":[{"type":"output_text","text":"answer","annotations":[annotation, annotation]}]}]}
    real = httpx.Client
    def reply(request):
        if request.method=='GET':
            return httpx.Response(200,json={'id':'file-abc','status':'completed','attributes':{'personal_llm_document_id':'file-abc'}})
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=result)
    monkeypatch.setattr(openai_api.httpx, "Client", lambda **kwargs: real(transport=httpx.MockTransport(reply), **kwargs))
    return seen, result


def test_search_wire_and_citations(search):
    seen, _ = search
    history = [HistoryMessage(role="user", content="before")]
    answer = core.process_message("question", history=history, vector_store_id="vs_allowed",document_ids=['file-abc'])
    assert answer.model_dump() == {"response":"answer","sources":[{"file_id":"file-abc","filename":"guide.txt"}]}
    assert seen[0]["tools"] == [{"type":"file_search","vector_store_ids":["vs_allowed"],"max_num_results":5,'filters':{'type':'in','key':'personal_llm_document_id','value':['file-abc']}}]
    assert seen[0]["input"] == [{"role":"user","content":"before"},{"role":"user","content":"question"}]
    assert seen[0]["store"] is False
    assert len(seen) == 1


def test_plain_chat_never_enables_search(search):
    seen, _ = search
    assert core.process_message("hello") == "answer"
    assert "tools" not in seen[0]


@pytest.mark.parametrize("store,status", [("vs_unknown",403),("",422),("https://evil",422),(123,422),("vs_allowed\n",422)])
def test_api_rejects_store_before_credentials(search,monkeypatch,store,status):
    monkeypatch.setattr(openai_api,"get_api_key",Mock(side_effect=AssertionError("No credentials")))
    with TestClient(app) as client:
        assert client.post("/chat",json={"message":"question","vector_store_id":store}).status_code==status
    assert not search[0]


def test_empty_allowlist_denies(search,monkeypatch):
    monkeypatch.delenv("OPENAI_ALLOWED_VECTOR_STORE_IDS")
    with pytest.raises(StoreForbidden):
        openai_api.generate_response("q",vector_store_id="vs_allowed")
    assert not search[0]


def test_search_api_response(search):
    with TestClient(app) as client:
        result=client.post("/chat",json={"message":"q","vector_store_id":"vs_allowed","document_ids":['file-abc']})
    assert result.status_code==200
    assert result.json()["sources"]==[{"file_id":"file-abc","filename":"guide.txt"}]


def test_mock_search_and_bedrock_rejection(search,monkeypatch):
    monkeypatch.setenv("HARNESS_BACKEND","mock")
    with TestClient(app) as client:
        assert client.post("/chat",json={"message":"q","vector_store_id":"vs_allowed","document_ids":['file-abc']}).json()=={"response":"Mock: q","sources":[]}
    monkeypatch.setenv("HARNESS_BACKEND","bedrock")
    monkeypatch.setenv("BEDROCK_MODEL_ID","test.model:0")
    with TestClient(app) as client:
        assert client.post("/chat",json={"message":"q","vector_store_id":"vs_allowed"}).status_code==422
    assert not search[0]


def test_no_annotations_is_valid(search):
    search[1]["output"][1]["content"][0]["annotations"]=[]
    assert core.process_message("q",vector_store_id="vs_allowed",document_ids=['file-abc']).sources==[]


def test_safe_source_extraction():
    annotations=[None,{"type":"url_citation","url":"javascript:bad"},
        {"type":"file_citation","file_id":"bad","filename":"secret"},
        {"type":"file_citation","file_id":"file-x","filename":False},
        {"type":"file_citation","file_id":"file-good","filename":"folder/safe.txt"}]
    output=[{"type":"message","content":[{"type":"output_text","annotations":annotations}]}]
    assert [c.model_dump() for c in extract_sources(output)]==[{"file_id":"file-good","filename":"safe.txt"}]
    output[0]["content"][0]["annotations"]=[{"type":"file_citation","file_id":f"file-{i}","filename":"x"*300} for i in range(30)]
    assert len(extract_sources(output))==20
    assert len(extract_sources(output)[0].filename)==255


def test_bridge_search_signed_and_preserves_sources(monkeypatch):
    from botocore.credentials import Credentials
    monkeypatch.setenv("OPENAI_ALLOWED_VECTOR_STORE_IDS","vs_allowed")
    monkeypatch.setattr(server.boto3,"Session",lambda:Mock(get_credentials=lambda:Credentials("fake","fake")))
    real=httpx.Client
    def reply(request):
        assert json.loads(request.content)["vector_store_id"]=="vs_allowed"
        assert request.headers["authorization"].startswith("AWS4-HMAC-SHA256")
        return httpx.Response(200,json={"response":"answer","sources":[{"file_id":"file-a","filename":"a.txt"}]})
    monkeypatch.setattr(server.httpx,"Client",lambda **kwargs:real(transport=httpx.MockTransport(reply),**kwargs))
    assert server.forward_message("q",vector_store_id="vs_allowed",document_ids=['file-a'])[1]["sources"][0]["file_id"]=="file-a"
    with pytest.raises(StoreForbidden):server.forward_message("q",vector_store_id="vs_unknown")
