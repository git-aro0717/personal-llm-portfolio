import os
from unittest.mock import patch
import pytest
from deploy import verify_file_search as probe


class FakeWire:
    def __init__(self,scenario='success'):
        self.files={};self.attached={};self.store=False;self.calls=[];self.scenario=scenario
    def call(self,method,path,**kwargs):
        self.calls.append((method,path))
        if path.startswith('/models/'):return 200,{'id':probe.MODEL}
        if path=='/vector_stores':self.store=True;return 200,{'id':'vs_test'}
        if path=='/files':
            if self.scenario=='upload_timeout':raise probe.ProbeError('timeout')
            identity='file-A' if not self.files else 'file-B'
            self.files[identity]=kwargs['files']['file'][0];return 200,{'id':identity}
        if method=='POST' and path.endswith('/files'):
            identity=kwargs['json']['file_id'];self.attached[identity]=kwargs['json']['attributes'];return 200,{'id':identity}
        if path=='/responses':
            identity=kwargs['json']['tools'][0]['filters']['value'][0]
            assert kwargs['json']['include']==['file_search_call.results']
            alpha=identity=='file-A'
            text='ALPHA-731 37 19 73 CITRINE-ALPHA-937' if alpha else 'BETA-842 82 46 128 INDIGO-BETA-684'
            result_id='file-B' if self.scenario=='contamination' and alpha else identity
            return 200,{'id':'resp-test','status':'completed','model':probe.MODEL,'usage':{'input_tokens':200,'output_tokens':50},
                'output':[{'type':'file_search_call','status':'completed','results':[{'file_id':result_id,'text':'not to be logged'}]},
                {'type':'message','content':[{'type':'output_text','text':text,'annotations':[{'type':'file_citation','file_id':identity,'filename':self.files[identity]}]}]}]}
        identity=path.rsplit('/',1)[-1]
        if method=='DELETE':
            if self.scenario=='delete_failure' and identity=='file-A' and path.startswith('/files/'):
                raise probe.ProbeError('network')
            if path.startswith('/files/'):
                self.files.pop(identity,None);self.attached.pop(identity,None)
            elif '/files/' in path:self.attached.pop(identity,None)
            else:self.store=False
            return 200,{'deleted':True}
        if path.startswith('/files/'):return (200,{'id':identity}) if identity in self.files else (404,{})
        if '/files/' in path:
            if identity not in self.attached:return 404,{}
            return 200,{'status':'failed' if self.scenario=='index_failure' else 'completed','attributes':self.attached[identity]}
        return (200,{'id':'vs_test'}) if self.store else (404,{})


@pytest.mark.parametrize('scenario,count,clean',[
    ('success',2,True),('contamination',1,True),('index_failure',0,True),
    ('upload_timeout',0,False),('delete_failure',2,False)])
def test_probe_offline(tmp_path,scenario,count,clean):
    fake=FakeWire(scenario);run=probe.Run(tmp_path/'run',wire=fake,new=True)
    with patch.dict(os.environ,{'OPENAI_API_KEY':'fake-key'}):
        os.environ.pop('AWS_LAMBDA_FUNCTION_NAME',None)
        data=run.execute(pause=lambda seconds:None)
    assert data['request_counts']['responses']==count
    assert data['cleanup']['complete']==clean
    if scenario=='success':
        assert data['status']=='tests_passed'
        assert data['removed_document_rejected_before_inference']
        assert data['search_tests'][0]['raw_result_ids']==['file-A']
        assert not fake.files and not fake.store
    else:assert data['status']=='failed' or not clean
    text=run.path.read_text()
    assert 'not to be logged' not in text and 'fake-key' not in text
    assert 'CITRINE-ALPHA-937' not in text
    if scenario=='delete_failure':
        # Explicit cleanup resumes from the saved ledger, without any inference.
        fake.scenario='success';resume=probe.Run(run.directory,wire=fake)
        before=sum(path=='/responses' for _,path in fake.calls)
        resume.cleanup();assert resume.data['cleanup']['complete']
        assert sum(path=='/responses' for _,path in fake.calls)==before


def test_interrupted_create_never_claims_cleanup_complete(tmp_path):
    run=probe.Run(tmp_path/'run',wire=FakeWire(),new=True)
    run.data['operations'].append({'method':'POST','path':'/vector_stores','status':'started'});run.save()
    resumed=probe.Run(run.directory,wire=FakeWire());resumed.cleanup()
    assert resumed.data['cleanup']['unknown_created_resources']
    assert not resumed.data['cleanup']['complete']


def test_hard_inference_limit(tmp_path):
    wire=FakeWire();run=probe.Run(tmp_path/'run',wire=wire,new=True)
    run.data['request_counts']['responses']=5
    with pytest.raises(probe.ProbeError,match='inference_limit'):run.call('POST','/responses',json={})
    assert not wire.calls


def test_locked_primary_keeps_recovery_ids_and_cleanup(monkeypatch,tmp_path):
    from pathlib import Path
    def denied(*args,**kwargs):raise PermissionError('simulated file lock')
    monkeypatch.setattr(Path,'replace',denied)
    fake=FakeWire();run=probe.Run(tmp_path/'run',wire=fake,new=True)
    # Primary file never appears, but each pre/post operation is durable.
    run.call('POST','/vector_stores',create='vector_stores',json={})
    assert not run.path.exists()
    recovered=probe.Run(run.directory,wire=fake)
    assert recovered.data['vector_stores'][0]['id']=='vs_test'
    recovered.cleanup()
    assert recovered.data['cleanup']['complete'] and not fake.store
    assert probe.Run(run.directory,wire=fake).data['cleanup']['complete']


def test_corrupt_snapshot_fails_closed(tmp_path):
    run=probe.Run(tmp_path/'run',wire=FakeWire(),new=True)
    (run.directory/'ledger-99999999-partial.json').write_text('{')
    with pytest.raises(probe.ProbeError,match='ledger_unreadable_stop'):
        probe.Run(run.directory,wire=FakeWire())


class Clock:
    def __init__(self):self.value=0
    def now(self):return self.value
    def sleep(self,seconds):self.value+=seconds


class DelayedDetach:
    def __init__(self,statuses,clock=None,list_pages=None):
        self.statuses=list(statuses);self.calls=[];self.clock=clock;self.list_pages=list(list_pages or [])
    def call(self,method,path,**kwargs):
        self.calls.append((method,path,kwargs))
        if method=='DELETE':return 200,{'deleted':True}
        if path.endswith('/files'):return 200,self.list_pages.pop(0)
        status=self.statuses.pop(0)
        return status,{'id':'file-A','vector_store_id':'vs_test','status':'completed',
                       'created_at':123,'usage_bytes':456,'attributes':{'private':'not recorded'},
                       'filename':'not recorded','last_error':{'message':'not recorded'}}


@pytest.mark.parametrize('statuses,confirmed', [([200,200,404],True),([200]*5,False)])
def test_detach_eventual_consistency(tmp_path,statuses,confirmed):
    clock=Clock();wire=DelayedDetach(statuses);run=probe.Run(tmp_path/'run',wire=wire,new=True)
    entry={'id':'file-A','detached':False}
    if confirmed:run.detach_and_verify('vs_test',entry,pause=clock.sleep,clock=clock.now)
    else:
        with pytest.raises(probe.ProbeError,match='detach_verification_timeout'):
            run.detach_and_verify('vs_test',entry,pause=clock.sleep,clock=clock.now)
    assert sum(method=='DELETE' for method,_,_ in wire.calls)==1
    assert sum(method=='GET' for method,_,_ in wire.calls)<=5
    assert clock.now()<=10 and entry['detached']==confirmed
    observation=run.data['detach_verifications'][0]
    assert observation['delete_accepted'] and observation['confirmed']==confirmed
    assert observation['get_observations'][0]['metadata']=={'id':'file-A','vector_store_id':'vs_test','status':'completed','created_at':123,'usage_bytes':456}
    assert 'not recorded' not in run.path.read_text()


def test_detach_paginated_list_is_diagnostic_only(tmp_path):
    clock=Clock();wire=DelayedDetach([200,404],list_pages=[
        {'data':[{'id':'file-other'}],'has_more':True,'last_id':'file-other'},
        {'data':[],'has_more':False}])
    run=probe.Run(tmp_path/'run',wire=wire,new=True);entry={'id':'file-A','detached':False}
    run.detach_and_verify('vs_test',entry,pause=clock.sleep,clock=clock.now,check_list=True)
    gets=[kwargs for method,path,kwargs in wire.calls if method=='GET' and path.endswith('/files')]
    assert gets[0]['params']=={'limit':100}
    assert gets[1]['params']=={'limit':100,'after':'file-other'}
    assert entry['detached'] and sum(m=='GET' for m,_,_ in wire.calls)==4


def test_detach_slow_get_uses_deadline(tmp_path):
    clock=Clock()
    class Slow(DelayedDetach):
        def call(self,method,path,**kwargs):
            result=super().call(method,path,**kwargs)
            if method=='GET':clock.sleep(11)
            return result
    wire=Slow([200]);run=probe.Run(tmp_path/'run',wire=wire,new=True)
    with pytest.raises(probe.ProbeError,match='detach_verification_timeout'):
        run.detach_and_verify('vs_test',{'id':'file-A'},clock=clock.now,pause=clock.sleep)
    assert len(wire.calls)==2
    assert run.data['detach_verifications'][0]['result']=='detach_verification_timeout'


class OriginalDeletion:
    def __init__(self,attachments,originals,pages=None,delete_success=True):
        self.attachments=list(attachments);self.originals=list(originals);self.pages=list(pages or [])
        self.calls=[];self.delete_success=delete_success
    def call(self,method,path,**kwargs):
        self.calls.append((method,path,kwargs))
        if method=='DELETE':return 200,{'deleted':self.delete_success}
        if path.endswith('/files'):return 200,self.pages.pop(0)
        status=(self.originals if path.startswith('/files/') else self.attachments).pop(0)
        return status,{'id':'file-B','status':'completed','attributes':{'private':'not recorded'}}


@pytest.mark.parametrize('attachments,originals,pages,confirmed',[
    ([200,404],[404],None,True),
    ([200]*4,[404],None,False),
    ([200,404],[404],[{'data':[],'has_more':False}],True),
    ([404],[200]*4,None,False),
])
def test_original_delete_eventual_consistency(tmp_path,attachments,originals,pages,confirmed):
    clock=Clock();wire=OriginalDeletion(attachments,originals,pages)
    run=probe.Run(tmp_path/'run',wire=wire,new=True);entry={'id':'file-B','detached':False}
    if confirmed:
        run.detach_and_verify('vs_test',entry,pause=clock.sleep,clock=clock.now,check_list=pages is not None,original=True)
    else:
        with pytest.raises(probe.ProbeError,match='original_delete_verification_timeout'):
            run.detach_and_verify('vs_test',entry,pause=clock.sleep,clock=clock.now,original=True)
    deletes=[path for method,path,_ in wire.calls if method=='DELETE']
    assert deletes==['/files/file-B']
    assert sum(method=='GET' for method,_,_ in wire.calls)<=5 and clock.now()<=10
    observation=run.data['original_delete_verifications'][0]
    assert observation['delete_accepted'] and observation['confirmed']==confirmed
    assert entry['original_delete_requested'] and entry['deleted']
    if confirmed:
        assert observation['attachment_confirmed_absent'] and observation['original_confirmed_absent']
        assert entry['absent_confirmed']
    else:assert observation['result']=='original_delete_verification_timeout'
    if pages is not None:
        # Original deletion reserves reads for both endpoints, not diagnostic lists.
        assert observation['get_observations'][0]['kind']=='original'
        assert observation['get_observations'][1]['kind']=='attachment'
        assert not any(path.endswith('/files') for method,path,_ in wire.calls if method=='GET')
    assert 'not recorded' not in run.path.read_text()


def test_original_delete_rejected_is_not_a_verification_timeout(tmp_path):
    wire=OriginalDeletion([],[],delete_success=False);run=probe.Run(tmp_path/'run',wire=wire,new=True)
    with pytest.raises(probe.ProbeError,match='original_delete_unconfirmed'):
        run.detach_and_verify('vs_test',{'id':'file-B'},original=True)
    assert len(wire.calls)==1


@pytest.mark.parametrize('attachments,originals,confirmed,original_absent,attachment_absent',[
    ([200,404],[404],True,True,True),
    ([404],[200,404],True,True,True),
    ([404],[404],True,True,True),
    ([200]*4,[404],False,True,False),
    ([404],[200]*4,False,False,True),
    ([200]*2,[200]*3,False,False,False),
])
def test_original_delete_independent_states(tmp_path,attachments,originals,confirmed,original_absent,attachment_absent):
    clock=Clock();wire=OriginalDeletion(attachments,originals)
    run=probe.Run(tmp_path/'run',wire=wire,new=True);entry={'id':'file-B'}
    if confirmed:run.detach_and_verify('vs_test',entry,pause=clock.sleep,clock=clock.now,original=True)
    else:
        with pytest.raises(probe.ProbeError,match='original_delete_verification_timeout'):
            run.detach_and_verify('vs_test',entry,pause=clock.sleep,clock=clock.now,original=True)
    obs=run.data['original_delete_verifications'][0]
    assert obs['confirmed']==confirmed
    assert obs['original_confirmed_absent']==original_absent
    assert obs['attachment_confirmed_absent']==attachment_absent
    assert [item['kind'] for item in obs['get_observations'][:2]]==['original','attachment']
    assert sum(m=='GET' for m,_,_ in wire.calls)<=5 and clock.now()<=10
    assert sum(m=='DELETE' for m,_,_ in wire.calls)==1
    if not confirmed:assert obs['verification_state']=='unconfirmed'
    resumed=probe.Run(run.directory,wire=wire)
    assert resumed.data['original_delete_verifications']==run.data['original_delete_verifications']


def test_original_delete_deadline_preserves_independent_success(tmp_path):
    clock=Clock()
    class Slow(OriginalDeletion):
        def call(self,method,path,**kwargs):
            result=super().call(method,path,**kwargs)
            if method=='GET' and not path.startswith('/files/'):clock.sleep(11)
            return result
    wire=Slow([200],[404]);run=probe.Run(tmp_path/'run',wire=wire,new=True)
    with pytest.raises(probe.ProbeError,match='original_delete_verification_timeout'):
        run.detach_and_verify('vs_test',{'id':'file-B'},clock=clock.now,pause=clock.sleep,original=True)
    obs=run.data['original_delete_verifications'][0]
    assert obs['original_confirmed_absent'] and not obs['attachment_confirmed_absent']
    assert obs['verification_state']=='unconfirmed' and len(wire.calls)==3


@pytest.mark.parametrize('status,body,category',[
    (403,{},'original_delete_verification_unexpected_status'),
    (500,{},'original_delete_verification_unexpected_status'),
    (200,[],'invalid_delete_metadata'),
    (200,{'id':'file-other'},'delete_metadata_mismatch'),
])
def test_original_delete_abnormal_response(tmp_path,status,body,category):
    class Invalid(OriginalDeletion):
        def call(self,method,path,**kwargs):
            if method=='GET':self.calls.append((method,path,kwargs));return status,body
            return super().call(method,path,**kwargs)
    wire=Invalid([],[]);run=probe.Run(tmp_path/'run',wire=wire,new=True)
    with pytest.raises(probe.ProbeError,match=category):
        run.detach_and_verify('vs_test',{'id':'file-B'},original=True)
    obs=run.data['original_delete_verifications'][0]
    assert not obs['confirmed'] and obs['verification_state']=='error'
    assert len(wire.calls)==2


@pytest.mark.parametrize('scenario,expected,clean',[
    ('success','tests_passed',True),
    ('attachment_pending','failed',True),
    ('index_failure','failed',True),
    ('upload_timeout','failed',False),
    ('delete_failure','failed',False),
])
def test_deletion_only_offline(tmp_path,scenario,expected,clean):
    class DeleteOnlyWire(FakeWire):
        def call(self,method,path,**kwargs):
            assert path!='/responses' and not path.startswith('/models/')
            if method=='DELETE' and path.startswith('/files/') and scenario=='attachment_pending':
                self.calls.append((method,path));self.files.pop(path.rsplit('/',1)[-1],None)
                return 200,{'deleted':True}
            return super().call(method,path,**kwargs)
    wire=DeleteOnlyWire(scenario);run=probe.Run(tmp_path/'deletion-only',wire=wire,new=True)
    # Any accidental production inference path must fail even before transport.
    with patch.object(run,'infer',side_effect=AssertionError('inference forbidden')):
        data=run.execute(pause=lambda seconds:None,deletion_only=True)
    assert data['status']==expected and data['cleanup']['complete']==clean
    assert data['verification_mode']=='deletion_only'
    assert data['request_counts']=={'responses':0,'file_search_calls_observed':0}
    assert sum(m=='POST' and p=='/vector_stores' for m,p in wire.calls)==1
    assert sum(m=='POST' and p=='/files' for m,p in wire.calls)==1
    assert len(data['files'])<=1
    deletes=[p for m,p in wire.calls if m=='DELETE']
    assert len(deletes)==len(set(deletes))
    assert len(list(run.directory.glob('ledger-*.json')))>1
    if scenario=='success':
        assert data['original_delete_verifications'][0]['confirmed']
        assert sum(m=='DELETE' and p.startswith('/files/') for m,p in wire.calls)==1
    if scenario=='attachment_pending':
        observation=data['original_delete_verifications'][0]
        assert observation['original_confirmed_absent'] and not observation['attachment_confirmed_absent']
        assert observation['verification_state']=='unconfirmed'
        assert data['failure']=='original_delete_verification_timeout'
    assert 'not recorded' not in run.path.read_text()


def test_deletion_only_blocks_inference_and_duplicate_run(tmp_path,monkeypatch,capsys):
    wire=FakeWire();run=probe.Run(tmp_path/'deletion-only',wire=wire,new=True)
    run.data['verification_mode']='deletion_only';run.save()
    with pytest.raises(probe.ProbeError,match='inference_forbidden'):run.call('POST','/responses',json={})
    assert not wire.calls and run.data['request_counts']['responses']==0
    before={p.name:p.read_bytes() for p in run.directory.iterdir()}
    monkeypatch.setenv('OPENAI_API_KEY','fake-key')
    monkeypatch.delenv('AWS_LAMBDA_FUNCTION_NAME',raising=False)
    monkeypatch.setattr('sys.argv',['probe','--deletion-only','--execute','--run-id','deletion-only','--ledger-root',str(tmp_path)])
    assert probe.main()==2
    assert before=={p.name:p.read_bytes() for p in run.directory.iterdir()}


def test_deletion_only_preview_is_offline(monkeypatch,capsys):
    import json
    monkeypatch.setattr('sys.argv',['probe','--deletion-only'])
    with patch.object(probe.Wire,'call',side_effect=AssertionError('network forbidden')):
        assert probe.main()==0
    preview=json.loads(capsys.readouterr().out)
    assert preview['planned_inference']==preview['hard_max_inference']==0
    assert preview['files']==preview['stores']==1 and preview['model'] is None
