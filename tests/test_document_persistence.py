import threading
from unittest.mock import Mock
import httpx
import pytest
from local_web import documents as docs, server
from local_web.deletion_store import DeletionStore, StateUnavailable
from app.harness import openai_api
from app.main import app
from fastapi.testclient import TestClient
from test_documents import FakeFiles


@pytest.fixture
def managed(monkeypatch,tmp_path):
    monkeypatch.setenv('OPENAI_ALLOWED_VECTOR_STORE_IDS','vs_allowed')
    monkeypatch.setenv('OPENAI_DOCUMENT_VECTOR_STORE_ID','vs_allowed')
    wire=FakeFiles();state=DeletionStore(tmp_path/'state.sqlite3')
    manager=docs.DocumentManager(wire,state=state,pause=lambda _:None)
    identity=manager.upload('fiction.txt',b'fiction')['id']
    wire.records[identity]['status']='completed'
    return manager,wire,state,identity


def test_restart_and_complete_tombstone(managed):
    manager,wire,state,identity=managed
    result=manager.delete(identity,'detach','fiction.txt')
    assert result['state']=='complete' and result['attachment_delete_accepted']
    assert result['attachment_last_http']==404
    calls=len(wire.calls)
    restarted=docs.DocumentManager(wire,state=state)
    assert len(wire.calls)==calls  # Construction never resumes external operations.
    assert restarted.listing()['deletions'][0]['state']=='complete'
    with pytest.raises(docs.DocumentError,match='not_found'):restarted.get(identity)
    assert restarted.delete(identity,'detach','fiction.txt')['state']=='complete'
    assert sum(m=='DELETE' for m,_,_ in wire.calls)==1


def test_block_is_durable_before_delete(managed):
    manager,wire,state,identity=managed;original=wire.call
    def observe(method,path,**kwargs):
        if method=='DELETE':
            restarted=docs.DocumentManager(wire,state=state)
            assert restarted.deletion_records()[identity]['state']=='deleting'
            with pytest.raises(docs.DocumentError,match='not_found'):restarted.ensure_selected([identity])
        return original(method,path,**kwargs)
    wire.call=observe
    manager.delete(identity,'detach','fiction.txt')


def test_db_failure_no_external_delete_and_normal_chat(managed,monkeypatch):
    manager,wire,state,identity=managed
    monkeypatch.setattr(state,'write',Mock(side_effect=StateUnavailable()))
    with pytest.raises(docs.DocumentError,match='document_state_unavailable'):manager.delete(identity,'detach','fiction.txt')
    assert not any(m=='DELETE' for m,_,_ in wire.calls)
    monkeypatch.setattr(state,'location',Mock(side_effect=StateUnavailable()))
    for operation in (manager.listing,lambda:manager.get(identity),lambda:manager.upload('a.txt',b'a'),lambda:manager.ensure_selected([identity])):
        with pytest.raises(docs.DocumentError,match='document_state_unavailable'):operation()
    monkeypatch.setattr(docs,'manager',manager)
    monkeypatch.setattr(server,'forward_message',lambda message:(200,{'response':'plain'}))
    with TestClient(server.app,base_url=server.ORIGIN,client=('127.0.0.1',12)) as client:
        token=client.get('/api/session').json()['csrf']
        response=client.post('/api/chat',json={'message':'q'},headers={'origin':server.ORIGIN,'x-csrf-token':token})
        assert response.status_code==200


def test_unconfirmed_resume_without_delete_replay(managed):
    manager,wire,state,identity=managed;original=wire.call
    def delayed(method,path,**kwargs):
        if method=='DELETE':wire.calls.append((method,path,kwargs));return {'deleted':True}
        return original(method,path,**kwargs)
    wire.call=delayed
    result=manager.delete(identity,'detach','fiction.txt')
    assert result['state']=='unconfirmed' and not result['detached']
    assert len(result['observations'])==5
    restarted=docs.DocumentManager(wire,state=state,pause=lambda _:None)
    with pytest.raises(docs.DocumentError):restarted.get(identity)
    wire.records.pop(identity)
    assert restarted.delete(identity,'detach','fiction.txt')['state']=='complete'
    assert sum(m=='DELETE' for m,_,_ in wire.calls)==1


def test_unacknowledged_intent_explicit_resume(managed):
    manager,wire,state,identity=managed
    with manager.transaction() as connection:
        state.write(connection,'vs_allowed',identity,{'state':'deleting','store':'vs_allowed','mode':'detach','name':'fiction.txt',
                    'detached':False,'original_deleted':False,'attachment_delete_sent':True})
    restarted=docs.DocumentManager(wire,state=state)
    before=len(wire.calls)
    assert restarted.delete(identity,'detach','fiction.txt')['state']=='complete'
    assert wire.calls[before][0]=='GET'  # Reconcile before explicit retry.


def test_chat_guard_serializes_delete_across_managers(managed):
    manager,wire,state,identity=managed;other=docs.DocumentManager(wire,state=state)
    entered=threading.Event();done=threading.Event();errors=[]
    def delete():
        entered.set()
        try:other.delete(identity,'detach','fiction.txt')
        except Exception as exc:errors.append(exc)
        finally:done.set()
    with manager.selection_guard([identity]):
        thread=threading.Thread(target=delete);thread.start();assert entered.wait(1)
        assert not done.wait(.1)
        assert not any(m=='DELETE' for m,_,_ in wire.calls)
    thread.join(3)
    assert done.is_set() and not errors
    with pytest.raises(docs.DocumentError):manager.ensure_selected([identity])


def test_original_delete_requires_usage_proof(managed):
    manager,wire,_,identity=managed
    with pytest.raises(docs.DocumentError,match='original_usage_unverified'):manager.delete(identity,'delete_original','fiction.txt')
    assert not any(m=='DELETE' for m,_,_ in wire.calls)
    assert manager.deletion_records()=={}


def test_delete_order_with_verified_usage(managed):
    manager,wire,_,identity=managed
    manager.original_usage_verified=lambda store,file:True
    result=manager.delete(identity,'delete_original','fiction.txt')
    assert result['state']=='complete' and result['original_deleted']
    actions=[(m,p) for m,p,_ in wire.calls if m in ('DELETE','GET')][-4:]
    assert actions==[('DELETE',f'/vector_stores/vs_allowed/files/{identity}'),('GET',f'/vector_stores/vs_allowed/files/{identity}'),
                     ('DELETE',f'/files/{identity}'),('GET',f'/files/{identity}')]


def test_store_only_rejected_before_credentials(monkeypatch):
    monkeypatch.setenv('HARNESS_BACKEND','mock')
    monkeypatch.setenv('OPENAI_ALLOWED_VECTOR_STORE_IDS','vs_allowed')
    monkeypatch.setattr(openai_api,'get_api_key',Mock(side_effect=AssertionError('credentials')))
    with TestClient(app) as client:
        assert client.post('/chat',json={'message':'q','vector_store_id':'vs_allowed'}).status_code==422


def test_lambda_original_absent_prevents_inference(monkeypatch):
    monkeypatch.setenv('OPENAI_ALLOWED_VECTOR_STORE_IDS','vs_allowed');monkeypatch.setenv('OPENAI_MODEL_ID','test')
    monkeypatch.setattr(openai_api,'get_api_key',lambda:'fake')
    real=httpx.Client;seen=[]
    def reply(request):
        seen.append(request)
        assert request.method=='GET'
        return httpx.Response(404 if request.url.path.startswith('/v1/files/') else 200,
                              json={'status':'completed','attributes':{'personal_llm_document_id':'file-a'}})
    monkeypatch.setattr(openai_api.httpx,'Client',lambda **kwargs:real(transport=httpx.MockTransport(reply),**kwargs))
    with pytest.raises(openai_api.OpenAIError,match='document_unavailable'):
        openai_api.generate_response('q',vector_store_id='vs_allowed',document_ids=['file-a'])
    assert len(seen)==2


def test_db_rejects_repository_and_corruption(tmp_path):
    with pytest.raises(StateUnavailable):
        with DeletionStore('state.sqlite3').transaction():pass
    broken=tmp_path/'broken.sqlite3';broken.write_bytes(b'not sqlite')
    with pytest.raises(StateUnavailable):
        with DeletionStore(broken).transaction():pass


def test_confirmation_deadline_keeps_tombstone(managed):
    manager,wire,_,identity=managed
    now=[0];manager.clock=lambda:now[0];manager.pause=lambda value:None
    original=wire.call
    def slow(method,path,**kwargs):
        if method=='DELETE':return {'deleted':True}
        if method=='GET' and '/vector_stores/' in path:now[0]+=11
        return original(method,path,**kwargs)
    wire.call=slow
    result=manager.delete(identity,'detach','fiction.txt')
    assert result['state']=='unconfirmed' and not result['detached']
    with pytest.raises(docs.DocumentError):manager.get(identity)


def test_delete_ack_not_confirmation_and_original_waits(managed):
    manager,wire,_,identity=managed;manager.original_usage_verified=lambda *_:True
    original=wire.call
    def pending(method,path,**kwargs):
        if method=='DELETE':wire.calls.append((method,path,kwargs));return {'deleted':True}
        return original(method,path,**kwargs)
    wire.call=pending
    result=manager.delete(identity,'delete_original','fiction.txt')
    assert result['attachment_delete_accepted'] and not result['detached']
    assert not any(m=='DELETE' and p.startswith('/files/') for m,p,_ in wire.calls)


def test_get_200_then_404_within_confirmation(managed):
    manager,wire,_,identity=managed;original=wire.call;remaining=[2]
    def delayed(method,path,**kwargs):
        if method=='DELETE':wire.calls.append((method,path,kwargs));return {'deleted':True}
        if method=='GET' and '/vector_stores/' in path:
            remaining[0]-=1
            if remaining[0]==0:wire.records.pop(identity)
        return original(method,path,**kwargs)
    wire.call=delayed
    result=manager.delete(identity,'detach','fiction.txt')
    assert result['state']=='complete'
    assert [item['http_status'] for item in result['observations']]==[200,404]


def test_delete_intent_write_failure_preserves_block_without_delete(managed,monkeypatch):
    manager,wire,state,identity=managed;write=state.write;calls=[0]
    def fail_second(*args):
        calls[0]+=1
        if calls[0]==2:raise StateUnavailable()
        return write(*args)
    monkeypatch.setattr(state,'write',fail_second)
    with pytest.raises(docs.DocumentError,match='document_state_unavailable'):manager.delete(identity,'detach','fiction.txt')
    assert not any(method=='DELETE' for method,_,_ in wire.calls)
    with pytest.raises(docs.DocumentError,match='not_found'):manager.ensure_selected([identity])


def test_store_file_composite_key_and_onedrive_denial(managed,tmp_path,monkeypatch):
    manager,_,state,identity=managed
    record={'state':'deleting','store':'vs_allowed','name':'fiction.txt','mode':'detach','detached':False,'original_deleted':False}
    with manager.transaction() as connection:
        state.write(connection,'vs_allowed',identity,record)
        state.write(connection,'vs_second',identity,{**record,'store':'vs_second'})
    with manager.transaction() as connection:
        assert state.read(connection,'vs_allowed')[identity]['store']=='vs_allowed'
        assert state.read(connection,'vs_second')[identity]['store']=='vs_second'
    monkeypatch.setenv('OneDrive',str(tmp_path/'cloud'))
    with pytest.raises(StateUnavailable):
        with DeletionStore(tmp_path/'cloud'/'state.sqlite3').transaction():pass
