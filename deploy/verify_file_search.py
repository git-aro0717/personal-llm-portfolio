"""Offline preview by default. Explicit execute/cleanup only; no AWS operations."""
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
import uuid
import queue
import threading

import httpx

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
MODEL='gpt-5.6-luna'
BASE='https://api.openai.com/v1'
FIXTURE_HASHES={
    'alpha-731.txt':'8304fbb8fd5d6f20009d9f80f4d1273c7c9c2d4b6ea61107421464bd52b20a71',
    'beta-842.txt':'7972c768c5f2b27b831cde91e0b85c6cc52b1ec17821b0d2b98af2370b736576'}
REAL_CLIENT=httpx.Client


class ProbeError(Exception):
    pass


class Wire:
    def call(self,method,path,**kwargs):
        deadline=kwargs.pop('_deadline',None)
        remaining=60 if deadline is None else min(60,deadline-time.monotonic())
        if remaining<=0:raise ProbeError('detach_verification_timeout')
        key=os.environ.get('OPENAI_API_KEY','')
        if not key.strip() or any(c.isspace() for c in key):raise ProbeError('local_key_missing_or_invalid')
        headers={'Authorization':'Bearer '+key}
        if path.startswith('/vector_stores'):headers['OpenAI-Beta']='assistants=v2'
        try:
            with REAL_CLIENT(timeout=httpx.Timeout(remaining,connect=min(10,remaining)),follow_redirects=False,trust_env=False,
                             transport=httpx.HTTPTransport(retries=0)) as client:
                with client.stream(method,BASE+path,headers=headers,**kwargs) as response:
                    status=response.status_code
                    if status==404:return status,{}
                    if status>=300:raise ProbeError({401:'authentication',403:'permission_denied',429:'rate_or_quota'}.get(status,'upstream_failure'))
                    content=bytearray()
                    for chunk in response.iter_bytes():
                        if deadline is not None and time.monotonic()>=deadline:raise ProbeError('detach_verification_timeout')
                        if len(content)+len(chunk)>1024*1024:raise ProbeError('response_too_large')
                        content.extend(chunk)
                    data=json.loads(content)
                    if not isinstance(data,dict):raise ProbeError('invalid_response')
                    return status,data
        except httpx.TimeoutException:raise ProbeError('timeout') from None
        except httpx.RequestError:raise ProbeError('network') from None
        except (ValueError,TypeError):raise ProbeError('invalid_response') from None


class Run:
    def __init__(self,directory,wire=None,new=False):
        self.directory=Path(directory);self.path=self.directory/'resources.json';self.wire=wire or Wire()
        if new:
            self.directory.mkdir(parents=True,exist_ok=False)
            self.data=json.loads((ROOT/'docs/e2e/v0.6.4/resource-ledger.template.json').read_text(encoding='utf-8'))
            self.data.update(run_id=self.directory.name,status='started',approved_budget_usd=0.10)
            self.save()
        else:
            # Recover IDs even when OneDrive prevented refreshing resources.json.
            candidates=[]
            for candidate in [self.path,*self.directory.glob('ledger-*.json')]:
                if candidate.exists():
                    try:candidates.append(json.loads(candidate.read_text(encoding='utf-8')))
                    except (ValueError,OSError):raise ProbeError('ledger_unreadable_stop') from None
            if not candidates:raise ProbeError('ledger_missing')
            self.data=max(candidates,key=lambda value:value.get('ledger_revision',0))
            if any(op.get('method')=='POST' and op.get('path') in ('/files','/vector_stores') and op.get('status') in ('started','unknown') for op in self.data['operations']):
                self.data['cleanup']['unknown_created_resources']=True

    def save(self):
        # First persist an immutable recovery snapshot. Replacing an existing
        # file can be denied by Windows/OneDrive even after writing succeeds.
        revision=self.data.get('ledger_revision',0)+1
        self.data['ledger_revision']=revision
        snapshot=self.directory/f'ledger-{revision:08d}-{uuid.uuid4().hex}.json'
        content=json.dumps(self.data,ensure_ascii=True,indent=2)
        with snapshot.open('x',encoding='utf-8') as output:
            output.write(content);output.flush();os.fsync(output.fileno())
        temp=self.directory/('resources-'+uuid.uuid4().hex+'.tmp')
        try:
            temp.write_text(content,encoding='utf-8')
            for attempt in range(3):
                try:
                    temp.replace(self.path)
                    break
                except PermissionError:
                    if attempt==2:
                        print('Ledger primary refresh blocked; recovery snapshot saved.',file=sys.stderr)
                    else:time.sleep(0.05)
        except OSError:
            print('Ledger primary refresh unavailable; recovery snapshot saved.',file=sys.stderr)
        finally:
            try:temp.unlink(missing_ok=True)
            except OSError:pass

    def call(self,method,path,create=None,**kwargs):
        if path!='/models/'+MODEL and not re.fullmatch(r'/[A-Za-z0-9_/-]+',path):raise ProbeError('invalid_path')
        if path=='/responses':
            if self.data.get('verification_mode')=='deletion_only':raise ProbeError('inference_forbidden')
            if self.data['request_counts']['responses']>=5:raise ProbeError('inference_limit')
            self.data['request_counts']['responses']+=1
        op={'number':len(self.data['operations'])+1,'method':method,'path':path,'status':'started'}
        self.data['operations'].append(op);self.save()
        try:
            status,body=self.wire.call(method,path,**kwargs)
            if create:
                identity=body.get('id');pattern=r'vs_[A-Za-z0-9_-]{1,128}' if create=='vector_stores' else r'file[-_][A-Za-z0-9_-]{1,128}'
                if status==404 or not isinstance(identity,str) or not re.fullmatch(pattern,identity):raise ProbeError('untracked_created_resource')
                entry={'id':identity,'deleted':False,'absent_confirmed':False}
                if create=='files':
                    entry.update(store_id=self.data['vector_stores'][0]['id'],attached=False,detached=False,fixture=kwargs['files']['file'][0],sha256=hashlib.sha256(kwargs['files']['file'][1]).hexdigest())
                self.data[create].append(entry)
            op.update(status='completed',http_status=status)
            self.save()
            return status,body
        except BaseException as exc:
            op.update(status='unknown' if create else 'failed',category=str(exc) if isinstance(exc,ProbeError) else type(exc).__name__)
            if create:self.data['cleanup']['unknown_created_resources']=True
            self.save()
            raise

    def cleanup(self,skip_attempted_deletes=False):
        unresolved=[]
        # Cleanup never guesses IDs, creates resources or invokes inference.
        for item in self.data['files']:
            try:
                if not re.fullmatch(r'file[-_][A-Za-z0-9_-]{1,128}',item['id']):raise ProbeError('invalid_ledger_id')
                attempted=skip_attempted_deletes and any(op.get('method')=='DELETE' and op.get('path')=='/files/'+item['id'] for op in self.data['operations'])
                if not item['deleted'] and not attempted:
                    status,body=self.call('DELETE','/files/'+item['id'])
                    if status!=404 and body.get('deleted') is not True:raise ProbeError('file_delete_unconfirmed')
                    item['deleted']=True;self.save()
                status,_=self.call('GET','/files/'+item['id'])
                item['absent_confirmed']=status==404
                if status!=404:raise ProbeError('file_still_present')
            except Exception:unresolved.append({'type':'file','id':item['id']})
            self.save()
        for item in self.data['vector_stores']:
            try:
                if not re.fullmatch(r'vs_[A-Za-z0-9_-]{1,128}',item['id']):raise ProbeError('invalid_ledger_id')
                if not item['deleted']:
                    status,body=self.call('DELETE','/vector_stores/'+item['id'])
                    if status!=404 and body.get('deleted') is not True:raise ProbeError('store_delete_unconfirmed')
                    item['deleted']=True;self.save()
                status,_=self.call('GET','/vector_stores/'+item['id'])
                item['absent_confirmed']=status==404
                if status!=404:raise ProbeError('store_still_present')
            except Exception:unresolved.append({'type':'vector_store','id':item['id']})
            self.save()
        unknown=self.data['cleanup']['unknown_created_resources']
        self.data['cleanup'].update(unresolved=unresolved,complete=not unresolved and not unknown)
        self.save()

    def execute(self,pause=time.sleep,deletion_only=False):
        try:
            if self.data['vector_stores'] or self.data['files'] or self.data['operations']:raise ProbeError('run_already_started')
            self.data['verification_mode']='deletion_only' if deletion_only else 'file_search'
            self.save()
            fixtures=('alpha-731.txt',) if deletion_only else tuple(FIXTURE_HASHES)
            for name in fixtures:
                expected=FIXTURE_HASHES[name]
                content=(ROOT/'docs/e2e/v0.6.4'/name).read_bytes().replace(b'\r\n',b'\n')
                if hashlib.sha256(content).hexdigest()!=expected:raise ProbeError('fixture_changed_stop')
            if not deletion_only:
                status,model=self.call('GET','/models/'+MODEL)
                if status!=200 or model.get('id')!=MODEL:raise ProbeError('model_unavailable')
            self.call('POST','/vector_stores',create='vector_stores',json={'name':'personal-llm-e2e-'+self.data['run_id'],'metadata':{'test_run':self.data['run_id']}})
            store=self.data['vector_stores'][0]['id']
            for name in fixtures:
                content=(ROOT/'docs/e2e/v0.6.4'/name).read_bytes()
                from local_web.documents import validate_file
                validate_file(name,content)
                self.call('POST','/files',create='files',data={'purpose':'assistants'},files={'file':(self.data['run_id']+'-'+name,content)})
                entry=self.data['files'][-1];identity=entry['id']
                status,_=self.call('POST',f'/vector_stores/{store}/files',json={'file_id':identity,'attributes':{'personal_llm_document_id':identity}})
                if status==404:raise ProbeError('registration_failed')
                entry['attached']=True;self.save()
                for attempt in range(12):
                    status,state=self.call('GET',f'/vector_stores/{store}/files/{identity}')
                    entry['index_status']=state.get('status');self.save()
                    if status!=200:raise ProbeError('registration_missing')
                    if state.get('status')=='completed':break
                    if state.get('status')!='in_progress':raise ProbeError('index_failed')
                    if attempt==11:raise ProbeError('index_timeout')
                    pause(10)
            if deletion_only:
                self.detach_and_verify(store,self.data['files'][0],pause=pause,original=True)
                self.data.update(original_delete_test='passed',status='tests_passed')
                return self.data
            self.infer(store)
            alpha,beta=self.data['files']
            self.detach_and_verify(store,alpha,pause=pause,check_list=True)
            status,_=self.call('GET','/files/'+alpha['id'])
            if status!=200:raise ProbeError('detach_removed_original')
            self.check_removed(store,alpha['id'])
            self.data['detach_test']='passed';self.save()
            # Removing original B must also remove its Store association.
            self.detach_and_verify(store,beta,pause=pause,check_list=True,original=True)
            self.data['original_delete_test']='passed'
            self.data['status']='tests_passed'
        except BaseException as exc:
            self.data.update(status='failed',failure=str(exc) if isinstance(exc,ProbeError) else type(exc).__name__)
        finally:
            # A failed final primary write must not skip the cleanup attempt.
            try:self.save()
            finally:self.cleanup(skip_attempted_deletes=deletion_only)
        return self.data

    def confirmation_get(self,path,deadline,clock,timeout_category='detach_verification_timeout',**kwargs):
        """Deadline-bound read only. Worker never changes the ledger."""
        remaining=deadline-clock()
        if remaining<=0:raise ProbeError(timeout_category)
        op={'number':len(self.data['operations'])+1,'method':'GET','path':path,'status':'started',
            'verification':'original_delete' if timeout_category.startswith('original_delete') else 'detach'}
        self.data['operations'].append(op);self.save()
        remaining=deadline-clock()
        if remaining<=0:
            op.update(status='failed',category=timeout_category);self.save()
            raise ProbeError(timeout_category)
        response_queue=queue.Queue(maxsize=1)
        def read():
            try:
                response_queue.put((True,self.wire.call('GET',path,_deadline=time.monotonic()+remaining,**kwargs)))
            except BaseException as exc:response_queue.put((False,exc))
        threading.Thread(target=read,daemon=True).start()
        try:
            ok,value=response_queue.get(timeout=remaining)
            if clock()>=deadline:raise queue.Empty()
            if not ok:raise value
            status,body=value
            op.update(status='completed',http_status=status);self.save()
            return status,body
        except BaseException as exc:
            timeout=isinstance(exc,queue.Empty) or (isinstance(exc,ProbeError) and str(exc) in ('timeout','detach_verification_timeout'))
            category=timeout_category if timeout else (str(exc) if isinstance(exc,ProbeError) else type(exc).__name__)
            op.update(status='failed',category=category);self.save()
            if timeout:raise ProbeError(timeout_category) from None
            raise

    def detach_and_verify(self,store,entry,pause=time.sleep,clock=time.monotonic,check_list=False,original=False):
        """One DELETE, at most five GETs (including list pages), ten-second budget."""
        if original:
            return self.original_delete_and_verify(store,entry,pause=pause,clock=clock)
        identity=entry['id'];path=f'/vector_stores/{store}/files/{identity}'
        timeout_category='original_delete_verification_timeout' if original else 'detach_verification_timeout'
        status,body=self.call('DELETE','/files/'+identity if original else path)
        accepted=status==200 and body.get('deleted') is True
        observation={'file_id':identity,'delete_http_status':status,'delete_accepted':accepted,
                     'already_absent':status==404,'get_observations':[],'confirmed':False}
        self.data.setdefault('original_delete_verifications' if original else 'detach_verifications',[]).append(observation)
        entry['original_delete_requested' if original else 'detach_requested']=accepted
        if original and (accepted or status==404):entry['deleted']=True
        self.save()
        if not accepted and status!=404:raise ProbeError('original_delete_unconfirmed' if original else 'detach_unconfirmed')
        start=clock();deadline=start+10
        list_next=False;list_used=False;cursor=None;seen_cursors=set();checking_original=False
        for attempt in range(5):
            if clock()>=deadline:break
            listing=list_next and not checking_original
            target=('/files/'+identity if checking_original else
                    f'/vector_stores/{store}/files' if listing else path)
            options={'params':{'limit':100,**({'after':cursor} if cursor else {})}} if listing else {}
            try:
                status,body=self.confirmation_get(target,deadline,clock,timeout_category=timeout_category,**options)
            except ProbeError as exc:
                if str(exc)==timeout_category:
                    observation.update(result=timeout_category,elapsed_seconds=round(clock()-start,3));self.save()
                raise
            if clock()>=deadline:
                observation.update(result=timeout_category,elapsed_seconds=round(clock()-start,3));self.save()
                raise ProbeError(timeout_category)
            item={'attempt':attempt+1,'http_status':status,'kind':'original' if checking_original else 'list' if listing else 'retrieve',
                  'elapsed_seconds':round(clock()-start,3)}
            observation['get_observations'].append(item)
            if not listing and status==404:
                if original and not checking_original:
                    observation['attachment_confirmed_absent']=True
                    entry['detached']=True;checking_original=True;list_next=False;self.save();continue
                if original:
                    observation['original_confirmed_absent']=True;entry['absent_confirmed']=True
                observation.update(confirmed=True,result='original_deleted' if original else 'detached',elapsed_seconds=round(clock()-start,3))
                entry['detached']=True;self.save();return
            if status!=200:raise ProbeError('detach_verification_unexpected_status')
            if not isinstance(body,dict):raise ProbeError('invalid_detach_metadata')
            if listing:
                records=body.get('data')
                if not isinstance(records,list) or any(not isinstance(record,dict) for record in records):raise ProbeError('invalid_detach_list')
                present=any(record.get('id')==identity for record in records)
                item.update(target_present_on_page=present,page_count=len(records),has_more=body.get('has_more') is True)
                if cursor:item['after']=cursor
                if body.get('has_more') is True and not present:
                    cursor=body.get('last_id')
                    if not isinstance(cursor,str) or not re.fullmatch(r'file[-_][A-Za-z0-9_-]{1,128}',cursor) or cursor in seen_cursors:
                        raise ProbeError('invalid_detach_list_cursor')
                    seen_cursors.add(cursor);list_next=True
                else:
                    item['list_scan_complete']=not present and not body.get('has_more')
                    list_next=False;cursor=None
            else:
                # Strict scalar allowlist; no attributes, filenames, contents, headers or errors.
                metadata={}
                for key,pattern in [('id',r'file[-_][A-Za-z0-9_-]{1,128}'),('vector_store_id',r'vs_[A-Za-z0-9_-]{1,128}')]:
                    value=body.get(key)
                    if isinstance(value,str) and re.fullmatch(pattern,value):metadata[key]=value
                if body.get('status') in ('in_progress','completed','failed','cancelled'):metadata['status']=body['status']
                for key in ('created_at','usage_bytes'):
                    if type(body.get(key)) is int and body[key]>=0:metadata[key]=body[key]
                item['metadata']=metadata
                if check_list and not list_used and not checking_original:list_next=True;list_used=True
            self.save()
            if attempt<4 and not list_next:
                wait=min(2,deadline-clock())
                if wait>0:pause(wait)
        observation.update(result=timeout_category,elapsed_seconds=round(clock()-start,3))
        self.save()
        raise ProbeError(timeout_category)

    def original_delete_and_verify(self,store,entry,pause=time.sleep,clock=time.monotonic):
        """Independently observe Files and Store association, sharing five reads/10s."""
        identity=entry['id'];category='original_delete_verification_timeout'
        status,body=self.call('DELETE','/files/'+identity)
        accepted=status==200 and body.get('deleted') is True
        states={kind:{'state':'unconfirmed','get_count':0} for kind in ('original','attachment')}
        observation={'file_id':identity,'delete_http_status':status,'delete_accepted':accepted,
                     'already_absent':status==404,'get_observations':[], 'states':states,
                     'confirmed':False,'original_confirmed_absent':False,'attachment_confirmed_absent':False}
        self.data.setdefault('original_delete_verifications',[]).append(observation)
        entry['original_delete_requested']=accepted
        if accepted or status==404:entry['deleted']=True
        self.save()
        if not accepted and status!=404:
            observation['result']='original_delete_unconfirmed';self.save()
            raise ProbeError('original_delete_unconfirmed')
        start=clock();deadline=start+10
        try:
            for attempt in range(5):
                if clock()>=deadline:break
                # Confirm both endpoints before repeating either; skip confirmed endpoints.
                kind=('original' if attempt%2==0 else 'attachment')
                if states[kind]['state']=='absent':kind='attachment' if kind=='original' else 'original'
                path='/files/'+identity if kind=='original' else f'/vector_stores/{store}/files/{identity}'
                states[kind]['get_count']+=1
                status,body=self.confirmation_get(path,deadline,clock,timeout_category=category)
                item={'attempt':attempt+1,'kind':kind,'http_status':status,'elapsed_seconds':round(clock()-start,3)}
                observation['get_observations'].append(item)
                states[kind].update(last_http_status=status,elapsed_seconds=item['elapsed_seconds'])
                if status==404:
                    states[kind]['state']='absent'
                    observation[kind+'_confirmed_absent']=True
                    entry['absent_confirmed' if kind=='original' else 'detached']=True
                elif status==200:
                    if not isinstance(body,dict):raise ProbeError('invalid_delete_metadata')
                    metadata={}
                    for key,expected in (('id',identity),('vector_store_id',store)):
                        if key in body and body[key]!=expected:raise ProbeError('delete_metadata_mismatch')
                        if body.get(key)==expected:metadata[key]=expected
                    if body.get('status') in ('in_progress','completed','failed','cancelled'):metadata['status']=body['status']
                    for key in ('created_at','usage_bytes'):
                        if type(body.get(key)) is int and body[key]>=0:metadata[key]=body[key]
                    item['metadata']=metadata;states[kind]['state']='observed_200'
                else:
                    states[kind]['state']='error'
                    raise ProbeError('original_delete_verification_unexpected_status')
                self.save()
                if all(state['state']=='absent' for state in states.values()):
                    observation.update(confirmed=True,verification_state='confirmed',result='original_deleted',elapsed_seconds=round(clock()-start,3))
                    self.save();return
                # First two reads reserve a slot for each endpoint. Later waits leave
                # time for the remaining reads instead of sleeping to the deadline.
                if attempt>=1 and attempt<4:
                    wait=min(2,max(0,(deadline-clock())/(5-attempt)))
                    if wait:pause(wait)
            raise ProbeError(category)
        except BaseException as exc:
            result=str(exc) if isinstance(exc,ProbeError) else type(exc).__name__
            observation.update(result=result,elapsed_seconds=round(clock()-start,3),
                               verification_state='unconfirmed' if result==category else 'error')
            self.save();raise

    def check_removed(self,store,identity):
        """Exercise adapter failure without allowing any further paid request."""
        from app.harness import openai_api
        runner=self
        class Removed(httpx.BaseTransport):
            def handle_request(self,request):
                if request.method!='GET' or str(request.url)!=BASE+f'/vector_stores/{store}/files/{identity}':
                    raise ProbeError('removed_document_triggered_inference')
                status,body=runner.call('GET',f'/vector_stores/{store}/files/{identity}')
                return httpx.Response(status,json=body)
        settings={'HARNESS_BACKEND':'openai','OPENAI_MODEL_ID':MODEL,'OPENAI_ALLOWED_VECTOR_STORE_IDS':store}
        before={k:os.environ.get(k) for k in settings};os.environ.update(settings)
        count=self.data['request_counts']['responses']
        try:
            openai_api.httpx.Client=lambda **kwargs:REAL_CLIENT(transport=Removed(),**kwargs)
            try:openai_api.generate_response('削除済み文書の確認',vector_store_id=store,document_ids=[identity])
            except openai_api.OpenAIError as exc:
                if str(exc)!='document_unavailable':raise ProbeError('unexpected_removed_document_error') from None
            else:raise ProbeError('removed_document_was_accepted')
            if self.data['request_counts']['responses']!=count:raise ProbeError('unexpected_extra_inference')
            self.data['removed_document_rejected_before_inference']=True;self.save()
        finally:
            openai_api.httpx.Client=REAL_CLIENT
            for k,v in before.items():
                if v is None:os.environ.pop(k,None)
                else:os.environ[k]=v

    def infer(self,store):
        from fastapi.testclient import TestClient
        from app.main import app
        from app.harness import openai_api
        expected=None;active=None;runner=self
        class Inspect(httpx.BaseTransport):
            def handle_request(self,request):
                if request.url.host!='api.openai.com' or request.url.scheme!='https':raise ProbeError('unexpected_target')
                path=request.url.path.removeprefix('/v1')
                if request.method=='GET':
                    if path not in (f'/vector_stores/{store}/files/{expected}',f'/files/{expected}'):raise ProbeError('unexpected_document_access')
                    status,body=runner.call('GET',path)
                else:
                    if request.method!='POST' or path!='/responses':raise ProbeError('unexpected_operation')
                    payload=json.loads(request.content)
                    tool=payload.get('tools',[{}])[0]
                    if payload.get('model')!=MODEL or tool.get('vector_store_ids')!=[store] or tool.get('filters')!={'type':'in','key':'personal_llm_document_id','value':[expected]}:
                        raise ProbeError('filter_mismatch')
                    payload['include']=['file_search_call.results']
                    status,body=runner.call('POST','/responses',json=payload)
                    runner.check_raw(body,expected,active)
                return httpx.Response(status,json=body)
        def factory(**kwargs):return REAL_CLIENT(transport=Inspect(),**kwargs)
        settings={'HARNESS_BACKEND':'openai','OPENAI_MODEL_ID':MODEL,'OPENAI_ALLOWED_VECTOR_STORE_IDS':store,
                  'OPENAI_MAX_OUTPUT_TOKENS':'512','OPENAI_TIMEOUT_SECONDS':'60'}
        previous={key:os.environ.get(key) for key in settings}
        os.environ.update(settings)
        try:
            if os.environ.get('AWS_LAMBDA_FUNCTION_NAME'):raise ProbeError('not_local_environment')
            self.data['search_tests']=[]
            with TestClient(app,raise_server_exceptions=False) as client:
                openai_api.httpx.Client=factory
                for entry in self.data['files']:
                    expected=entry['id'];active={'file_id':expected};self.data['search_tests'].append(active)
                    question='選択資料の製品識別子、測定周期、連続稼働時間、最大測定距離、校正コードを短く列挙し、出典を付けてください。資料にない他製品の仕様は答えないでください。'
                    start=time.monotonic()
                    response=client.post('/chat',json={'message':question,'history':[], 'vector_store_id':store,'document_ids':[expected]})
                    active.update(backend_http_status=response.status_code,seconds=round(time.monotonic()-start,3))
                    if response.status_code!=200:raise ProbeError('backend_search_failed')
                    data=response.json();answer=data.get('response','')
                    alpha='alpha-731' in entry['fixture']
                    own=['ALPHA-731','37','19','73','CITRINE-ALPHA-937'] if alpha else ['BETA-842','82','46','128','INDIGO-BETA-684']
                    foreign=['BETA-842','INDIGO-BETA-684'] if alpha else ['ALPHA-731','CITRINE-ALPHA-937']
                    if not all(value in answer for value in own) or any(value in answer for value in foreign):raise ProbeError('answer_mismatch_or_contamination')
                    if not data.get('sources') or any(source['file_id']!=expected for source in data['sources']):raise ProbeError('citation_mismatch')
                    # Do not persist document text or model answer content.
                    active.update(answer_values_match=True,source_ids=[source['file_id'] for source in data['sources']],passed=True)
                    self.save()
        finally:
            openai_api.httpx.Client=REAL_CLIENT
            for key,value in previous.items():
                if value is None:os.environ.pop(key,None)
                else:os.environ[key]=value

    def check_raw(self,body,expected,active):
        if body.get('model')!=MODEL:raise ProbeError('unexpected_model')
        if body.get('id'):self.data['response_ids'].append(body['id'])
        searches=[item for item in body.get('output',[]) if item.get('type')=='file_search_call']
        ids=[item.get('file_id') for search in searches for item in search.get('results',[])]
        annotations=[annotation for item in body.get('output',[]) if item.get('type')=='message'
                     for block in item.get('content',[]) for annotation in block.get('annotations',[])
                     if annotation.get('type')=='file_citation']
        self.data['request_counts']['file_search_calls_observed']+=len(searches)
        active.update(raw_result_ids=ids,raw_citation_ids=[a.get('file_id') for a in annotations])
        usage=body.get('usage',{})
        active['usage']={k:v for k,v in usage.items() if k in ('input_tokens','output_tokens','total_tokens') and type(v) is int}
        self.save()
        if len(searches)!=1 or searches[0].get('status')!='completed' or not ids or any(identity!=expected for identity in ids):raise ProbeError('raw_search_contamination_or_missing')
        if not annotations or any(a.get('file_id')!=expected for a in annotations):raise ProbeError('raw_citation_contamination_or_missing')
        filename=next(item['fixture'] for item in self.data['files'] if item['id']==expected)
        if any(a.get('filename')!=filename for a in annotations):raise ProbeError('raw_citation_filename_mismatch')
        active['citation_filename_matches']=True
        if any(type(active['usage'].get(k)) is not int or active['usage'][k]<0 for k in ('input_tokens','output_tokens')):raise ProbeError('usage_unavailable')
        estimate=sum(t.get('usage',{}).get('input_tokens',0)*0.20/1e6+t.get('usage',{}).get('output_tokens',0)*1.20/1e6 for t in self.data['search_tests'])+self.data['request_counts']['file_search_calls_observed']*0.0025
        self.data['estimated_inference_usd']=estimate
        if estimate>0.05:raise ProbeError('unexpected_cost_stop')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    mode=parser.add_mutually_exclusive_group()
    mode.add_argument('--execute',action='store_true');mode.add_argument('--cleanup',action='store_true')
    parser.add_argument('--run-id',default='file-search-1')
    parser.add_argument('--ledger-root',type=Path,help='Optional local directory outside OneDrive; reuse it for cleanup')
    parser.add_argument('--deletion-only',action='store_true',help='One TXT/Store, no model lookup or inference')
    args=parser.parse_args()
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}',args.run_id):parser.error('Invalid run ID')
    if not args.execute and not args.cleanup:
        print(json.dumps({'mode':'preview','verification_mode':'deletion_only' if args.deletion_only else 'file_search',
                          'model':None if args.deletion_only else MODEL,'planned_inference':0 if args.deletion_only else 2,
                          'hard_max_inference':0 if args.deletion_only else 5,'stores':1,'files':1 if args.deletion_only else 2,
                          'retries':0,'budget_target_usd':0.10}));return 0
    key=os.environ.get('OPENAI_API_KEY','')
    if not key.strip() or any(c.isspace() for c in key) or os.environ.get('AWS_LAMBDA_FUNCTION_NAME'):
        print('Stopped: missing/invalid local key or non-local environment');return 2
    directory=(args.ledger_root or ROOT/'.aws-sam/v064-e2e')/args.run_id
    if args.execute and directory.exists():print('Stopped: run already exists; do not retry creation');return 2
    if args.cleanup and not (directory/'resources.json').exists() and not list(directory.glob('ledger-*.json')):print('Stopped: no ledger');return 2
    run=Run(directory,new=args.execute)
    if args.execute:run.execute(deletion_only=args.deletion_only)
    else:run.cleanup()
    print(json.dumps({'run_id':args.run_id,'status':run.data['status'],'request_counts':run.data['request_counts'],
                      'failure':run.data.get('failure'),'cleanup':run.data['cleanup'],'estimated_inference_usd':run.data.get('estimated_inference_usd')}))
    return 0 if run.data['cleanup']['complete'] and (args.cleanup or run.data['status']=='tests_passed') else 1


if __name__=='__main__':raise SystemExit(main())
