import json
from unittest.mock import Mock
import httpx
import pytest
from fastapi.testclient import TestClient
from app.harness import core, openai_api
from local_web import documents as docs, server


class FakeFiles:
    def __init__(self):
        self.files={};self.records={};self.calls=[];self.fail=None
    def call(self,method,path,**kwargs):
        self.calls.append((method,path,kwargs))
        if self.fail==path:raise docs.DocumentError('network',503)
        identity=path.rsplit('/',1)[-1]
        if method=='POST' and path=='/files':
            identity=f'file-{len(self.files)+1}';self.files[identity]=kwargs['files']['file'][0];return {'id':identity}
        if method=='POST':
            identity=kwargs['json']['file_id'];self.records[identity]={'id':identity,'status':'in_progress','attributes':kwargs['json']['attributes']};return self.records[identity]
        if method=='GET' and path.endswith('/files'):
            return {'data':list(self.records.values()),'has_more':False}
        if method=='GET':
            if '/vector_stores/' in path:
                if identity not in self.records:raise docs.DocumentError('not_found',404)
                return self.records[identity]
            if identity not in self.files:raise docs.DocumentError('not_found',404)
            return {'id':identity,'filename':self.files[identity]}
        if method=='DELETE':
            target=self.records if '/vector_stores/' in path else self.files
            if identity not in target:raise docs.DocumentError('not_found',404)
            del target[identity];return {'deleted':True}


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setenv('OPENAI_ALLOWED_VECTOR_STORE_IDS','vs_allowed')
    monkeypatch.setenv('OPENAI_DOCUMENT_VECTOR_STORE_ID','vs_allowed')
    fake=FakeFiles();manager=docs.DocumentManager(fake,original_usage_verified=lambda store,identity:True)
    monkeypatch.setattr(docs,'manager',manager)
    client=TestClient(server.app,base_url=server.ORIGIN,client=('127.0.0.1',123))
    token=client.get('/api/session').json()['csrf']
    client.headers.update({'origin':server.ORIGIN,'x-csrf-token':token})
    return client,manager,fake


def upload(client,name='hello.txt',data=b'hello'):
    return client.post('/api/documents',params={'filename':name},content=data,headers={'content-type':'application/octet-stream'})


def test_upload_list_status_select_delete(setup,monkeypatch):
    client,manager,fake=setup
    response=upload(client);assert response.status_code==202
    identity=response.json()['id'];assert response.json()['status']=='in_progress'
    assert client.get('/api/documents').json()['documents'][0]['selectable'] is False
    assert client.post('/api/chat',json={'message':'q','document_ids':[identity]}).status_code==409
    fake.records[identity]['status']='completed'
    assert client.get('/api/documents/'+identity).json()['selectable']
    seen=[]
    monkeypatch.setattr(server,'forward_message',lambda *args:(seen.append(args) or (200,{'response':'answer','sources':[]})))
    assert client.post('/api/chat',json={'message':'q','document_ids':[identity]}).status_code==200
    assert seen[0][2:] == ('vs_allowed',[identity])
    body={'mode':'delete_original','confirmation':'hello.txt'}
    result=client.post(f'/api/documents/{identity}/delete',json=body)
    assert result.status_code==200 and result.json()['original_deleted']
    calls=len(fake.calls)
    assert client.post(f'/api/documents/{identity}/delete',json=body).status_code==200
    assert len(fake.calls)==calls
    assert client.get('/api/documents').json()['documents']==[]
    assert client.post('/api/chat',json={'message':'q','document_ids':[identity]}).status_code==404
    assert len(seen)==1


@pytest.mark.parametrize('name,data',[('a.pdf',b'%PDF-1.4\nbody\n%%EOF'),('a.txt','日本語'.encode()),('a.md',b'# title')])
def test_formats(setup,name,data):assert upload(setup[0],name,data).status_code==202


@pytest.mark.parametrize('name,data',[('../a.txt',b'hi'),('a.exe',b'hi'),('a.pdf',b'not pdf'),('a.txt',b'\x00'),('a.txt',b'\xff'),('a.txt',b''),('a\n.txt',b'hi')])
def test_invalid_files_no_upstream(setup,name,data):
    response=upload(setup[0],name,data);assert response.status_code in (422,413);assert not setup[2].calls


def test_oversize_and_csrf(setup):
    client,_,fake=setup
    assert upload(client,data=b'x'*(docs.MAX_FILE_BYTES+1)).status_code==413
    client.headers['x-csrf-token']='wrong'
    assert upload(client).status_code==403
    assert client.post('/api/documents/file-1/delete',json={}).status_code==403
    assert not fake.calls


def test_forbidden_store(setup,monkeypatch):
    monkeypatch.setenv('OPENAI_DOCUMENT_VECTOR_STORE_ID','vs_other')
    assert setup[0].get('/api/documents').status_code==403
    assert upload(setup[0]).status_code==403
    assert not setup[2].calls


def test_failed_index_and_confirmation(setup):
    client,manager,fake=setup;identity=upload(client).json()['id']
    fake.records[identity]['status']='failed'
    assert client.get('/api/documents/'+identity).json()['status']=='failed'
    assert client.post('/api/chat',json={'message':'q','document_ids':[identity]}).status_code==409
    assert client.post(f'/api/documents/{identity}/delete',json={'mode':'detach','confirmation':'wrong'}).status_code==422
    assert identity in fake.records


def test_partial_registration_and_original_delete(setup):
    client,manager,fake=setup;fake.fail='/vector_stores/vs_allowed/files'
    result=upload(client);assert result.status_code==502
    identity=result.json()['file_id'];assert identity in fake.files
    fake.fail=None
    assert client.get('/api/documents').json()['documents'][0]['status']=='failed'
    assert client.post(f'/api/documents/{identity}/delete',json={'mode':'delete_original','confirmation':'hello.txt'}).status_code==200


def test_partial_delete_retry(setup):
    client,manager,fake=setup;identity=upload(client).json()['id']
    fake.fail=f'/files/{identity}';body={'mode':'delete_original','confirmation':'hello.txt'}
    result=client.post(f'/api/documents/{identity}/delete',json=body)
    assert result.status_code==503 and result.json()['detail']=='detached_original_delete_failed'
    assert identity not in fake.records and identity in fake.files
    fake.fail=None
    assert client.post(f'/api/documents/{identity}/delete',json=body).status_code==200
    assert sum(method=='DELETE' and '/vector_stores/' in path for method,path,_ in fake.calls)==1


def test_detach_keeps_original_and_unowned_original_denied(setup):
    client,manager,fake=setup;identity=upload(client).json()['id']
    manager.owned.clear()
    assert client.post(f'/api/documents/{identity}/delete',json={'mode':'delete_original','confirmation':'hello.txt'}).status_code==403
    assert client.post(f'/api/documents/{identity}/delete',json={'mode':'detach','confirmation':'hello.txt'}).status_code==200
    assert identity in fake.files


@pytest.mark.parametrize('status,expected',[(401,403),(429,429),(500,502),(302,502),(404,404)])
def test_client_error_redaction(monkeypatch,status,expected):
    monkeypatch.setenv('OPENAI_API_KEY','fake-key');real=httpx.Client;seen=[]
    def reply(request):seen.append(request);return httpx.Response(status,json={'secret':'never-visible'})
    monkeypatch.setattr(docs.httpx,'Client',lambda **kwargs:real(transport=httpx.MockTransport(reply),**kwargs))
    with pytest.raises(docs.DocumentError) as exc:docs.OpenAIFilesClient().call('GET','/files/file-a')
    assert exc.value.status==expected and 'never-visible' not in str(exc.value) and len(seen)==1


@pytest.mark.parametrize('error,status',[(httpx.ReadTimeout('private'),504),(httpx.ConnectError('private'),503)])
def test_client_transport_failure(monkeypatch,error,status):
    monkeypatch.setenv('OPENAI_API_KEY','fake-key');real=httpx.Client
    def reply(request):raise error
    monkeypatch.setattr(docs.httpx,'Client',lambda **kwargs:real(transport=httpx.MockTransport(reply),**kwargs))
    with pytest.raises(docs.DocumentError) as exc:docs.OpenAIFilesClient().call('GET','/files/file-a')
    assert exc.value.status==status and 'private' not in str(exc.value)


def test_openai_filter_and_deleted_selection(monkeypatch):
    monkeypatch.setenv('HARNESS_BACKEND','openai');monkeypatch.setenv('OPENAI_MODEL_ID','test')
    monkeypatch.setenv('OPENAI_ALLOWED_VECTOR_STORE_IDS','vs_allowed')
    monkeypatch.setattr(openai_api,'get_api_key',lambda:'fake-key')
    real=httpx.Client;seen=[];deleted=False
    def reply(request):
        seen.append(request)
        if request.method=='GET':return httpx.Response(404 if deleted else 200,json={'status':'completed','attributes':{docs.ATTRIBUTE:'file-a'}})
        payload=json.loads(request.content)
        assert payload['tools'][0]['filters']=={'type':'in','key':docs.ATTRIBUTE,'value':['file-a']}
        return httpx.Response(200,json={'status':'completed','output':[{'type':'message','content':[{'type':'output_text','text':'answer','annotations':[]}]}]})
    monkeypatch.setattr(openai_api.httpx,'Client',lambda **kwargs:real(transport=httpx.MockTransport(reply),**kwargs))
    assert core.process_message('q',vector_store_id='vs_allowed',document_ids=['file-a']).response=='answer'
    deleted=True
    with pytest.raises(openai_api.OpenAIError):core.process_message('q',vector_store_id='vs_allowed',document_ids=['file-a'])
    assert sum(r.method=='POST' for r in seen)==1


def test_files_wire_upload_register_and_list(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY','fake-key')
    monkeypatch.setenv('OPENAI_DOCUMENT_VECTOR_STORE_ID','vs_allowed')
    monkeypatch.setenv('OPENAI_ALLOWED_VECTOR_STORE_IDS','vs_allowed')
    real=httpx.Client;seen=[]
    def reply(request):
        seen.append(request)
        assert request.url.host=='api.openai.com' and request.url.scheme=='https'
        assert request.headers['authorization']=='Bearer fake-key'
        if request.url.path=='/v1/files':
            assert request.method=='POST' and b'assistants' in request.content and b'hello' in request.content
            return httpx.Response(200,json={'id':'file-wire'})
        if request.method=='POST':
            assert json.loads(request.content)=={'file_id':'file-wire','attributes':{docs.ATTRIBUTE:'file-wire'}}
            return httpx.Response(200,json={'status':'in_progress'})
        if request.url.path.endswith('/files'):
            return httpx.Response(200,json={'data':[{'id':'file-wire','status':'completed','attributes':{docs.ATTRIBUTE:'file-wire'}}],'has_more':False})
        return httpx.Response(200,json={'filename':'hello.txt'})
    def factory(**kwargs):
        assert kwargs['follow_redirects'] is False and kwargs['trust_env'] is False
        return real(transport=httpx.MockTransport(reply),**kwargs)
    monkeypatch.setattr(docs.httpx,'Client',factory)
    manager=docs.DocumentManager(docs.OpenAIFilesClient())
    assert manager.upload('hello.txt',b'hello')['status']=='in_progress'
    assert manager.listing()['documents'][0]['selectable']
    assert len(seen)==4


@pytest.mark.parametrize('body',[{'mode':[],'confirmation':'hello.txt'}, {'mode':'erase','confirmation':'hello.txt'}, {'mode':'detach','confirmation':123}])
def test_invalid_delete_request(setup,body):
    client,_,fake=setup;identity=upload(client).json()['id']
    assert client.post(f'/api/documents/{identity}/delete',json=body).status_code==422
    assert identity in fake.records
