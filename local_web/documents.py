"""Fixed OpenAI endpoints with durable local deletion metadata; no document storage."""
import os
import re
import threading
import json
import time
import queue
from contextlib import contextmanager
from local_web.deletion_store import DeletionStore, StateUnavailable
from local_web.csv_documents import csv_to_text, CSVError
from typing import Protocol

import httpx
from app.file_search import authorize_store

MAX_FILE_BYTES = 5 * 1024 * 1024
FILE_PATTERN = r"file[-_][A-Za-z0-9_-]{1,128}"
ATTRIBUTE = "personal_llm_document_id"


class DocumentError(Exception):
    def __init__(self, category, status=502, file_id=None):
        self.category, self.status, self.file_id = category, status, file_id
        super().__init__(category)


def file_id(value):
    if not isinstance(value, str) or not re.fullmatch(FILE_PATTERN, value):
        raise DocumentError("invalid_file_id", 422)
    return value


def store_id():
    value = os.environ.get("OPENAI_DOCUMENT_VECTOR_STORE_ID", "")
    authorize_store(value)
    return value


def validate_file(name, data):
    if not isinstance(name, str) or not name or len(name) > 180 or any(c in name for c in '/\\') or any(not c.isprintable() for c in name):
        raise DocumentError("invalid_filename", 422)
    suffix = name.rsplit('.', 1)[-1].lower()
    if suffix not in {"pdf", "txt", "md", "csv"}:
        raise DocumentError("unsupported_format", 422)
    if suffix == 'csv' and len(name) > 176:
        # Leave room for .txt within the existing 180-character display limit.
        raise DocumentError('invalid_filename', 422)
    if not data or len(data) > MAX_FILE_BYTES:
        raise DocumentError("invalid_file_size", 413)
    if suffix == 'pdf':
        if not data.startswith(b'%PDF-') or b'%%EOF' not in data[-1024:]:
            raise DocumentError("invalid_pdf", 422)
    else:
        try:
            text = data.decode('utf-8-sig')
        except UnicodeError:
            raise DocumentError("invalid_text_encoding", 422) from None
        if not text.strip() or any(ord(c) < 32 and c not in '\r\n\t' for c in text):
            raise DocumentError("invalid_text", 422)
    return name


class FilesClient(Protocol):
    def call(self, method, path, **kwargs) -> dict: ...


class OpenAIFilesClient:
    def call(self, method, path, **kwargs):
        # Paths are constructed only by DocumentManager after ID validation.
        key = os.environ.get('OPENAI_API_KEY', '')
        if not key.strip() or any(c.isspace() for c in key):
            raise DocumentError('credentials_unavailable', 403)
        timeout=kwargs.pop('_timeout',60)
        try:
            with httpx.Client(timeout=httpx.Timeout(timeout, connect=min(10,timeout)), follow_redirects=False, trust_env=False) as client:
                with client.stream(method, 'https://api.openai.com/v1'+path,
                                   headers={'Authorization':'Bearer '+key}, **kwargs) as response:
                    if response.status_code == 404:
                        raise DocumentError('not_found', 404)
                    if response.status_code in (401,403):
                        raise DocumentError('authentication', 403)
                    if response.status_code == 429:
                        raise DocumentError('rate_or_quota_limit', 429)
                    if response.status_code >= 300:
                        raise DocumentError('upstream_failure')
                    data = bytearray()
                    for chunk in response.iter_bytes():
                        if len(data)+len(chunk)>1024*1024:
                            raise DocumentError('invalid_upstream_response')
                        data.extend(chunk)
                    result = json.loads(data)
                    if not isinstance(result,dict):raise ValueError()
                    return result
        except httpx.TimeoutException:
            raise DocumentError('timeout', 504) from None
        except httpx.RequestError:
            raise DocumentError('network', 503) from None
        except (ValueError,TypeError):
            raise DocumentError('invalid_upstream_response') from None


class DocumentManager:
    def __init__(self, client: FilesClient, state=None, original_usage_verified=None,clock=time.monotonic,pause=time.sleep):
        self.client=client
        self.owned={}  # Only files uploaded in this local process can be globally deleted.
        self.deletions={}
        self.lock=threading.RLock()
        self.state=state or DeletionStore()
        # No OpenAI endpoint proves absence of all other uses. Fail closed by default.
        self.original_usage_verified=original_usage_verified or (lambda store,identity:False)
        self.clock,self.pause=clock,pause

    def confirm_absent(self,path,operation,kind):
        start=self.clock();deadline=start+10
        for attempt in range(5):
            remaining=deadline-self.clock()
            if remaining<=0:break
            result_queue=queue.Queue(maxsize=1)
            def read():
                try:result_queue.put((self.client.call('GET',path,_timeout=remaining),None))
                except Exception as exc:result_queue.put((None,exc))
            threading.Thread(target=read,daemon=True).start()
            try:_,error=result_queue.get(timeout=remaining)
            except queue.Empty:break
            if self.clock()>=deadline:break
            if error and (not isinstance(error,DocumentError) or error.status!=404):raise error
            http=404 if error else 200
            operation.setdefault('observations',[]).append({'kind':kind,'http_status':http,'elapsed_seconds':round(self.clock()-start,3)})
            operation[kind+'_last_http']=http
            if http==404:
                operation[kind+'_verification_elapsed']=round(self.clock()-start,3)
                return True
            if attempt<4:self.pause(min(1,max(0,deadline-self.clock())))
        operation[kind+'_verification_elapsed']=round(self.clock()-start,3)
        operation[kind+'_verification_result']='unconfirmed'
        return False

    @contextmanager
    def transaction(self):
        try:
            with self.state.transaction() as connection:yield connection
        except StateUnavailable:raise DocumentError('document_state_unavailable',503) from None

    def deletion_records(self):
        with self.transaction() as connection:return self.state.read(connection,store_id())

    @contextmanager
    def selection_guard(self,identities):
        # BEGIN IMMEDIATE also serializes other relay processes using the same DB.
        with self.lock, self.transaction() as connection:
            blocked=self.state.read(connection,store_id())
            if any(identity in blocked for identity in identities):raise DocumentError('not_found',404)
            for identity in identities:
                if not self.get(identity,_blocked=blocked)['selectable']:raise DocumentError('document_not_ready',409)
            yield

    def upload(self,name,data):
        store=store_id();validate_file(name,data)
        if name.rsplit('.',1)[-1].lower()=='csv':
            try:data=csv_to_text(name,data,max_bytes=MAX_FILE_BYTES)
            except CSVError as exc:raise DocumentError(exc.category,exc.status) from None
            # All API views and deletion confirmations use the same remote name.
            name=name+'.txt'
        self.deletion_records()  # Fail closed before upload if durable state is unavailable.
        with self.lock:
            if len(self.owned)>=200:raise DocumentError('session_file_limit',409)
            uploaded=self.client.call('POST','/files',data={'purpose':'assistants'},files={'file':(name,data)})
            identity=file_id(uploaded.get('id'))
            self.owned[identity]={'name':name,'store':store,'attached':False}
            try:
                result=self.client.call('POST',f'/vector_stores/{store}/files',json={'file_id':identity,'attributes':{ATTRIBUTE:identity}})
            except DocumentError:
                # Upload succeeded; do not blindly retry or silently delete a charged upload.
                raise DocumentError('registration_failed_original_retained',502,identity) from None
            self.owned[identity]['attached']=True
            return self.view(identity,result,name)

    def view(self,identity,record,name):
        if not isinstance(record,dict):raise DocumentError('invalid_upstream_response')
        state=record.get('status')
        if state not in {'in_progress','completed','failed','cancelled'}:
            raise DocumentError('invalid_upstream_response')
        safe=''.join(c for c in str(name).replace('\\','/').rsplit('/',1)[-1] if c.isprintable())[:180]
        attributes=record.get('attributes') or {}
        return {'id':identity,'filename':safe,'status':state,'selectable':state=='completed' and isinstance(attributes,dict) and attributes.get(ATTRIBUTE)==identity,
                'can_delete_original':identity in self.owned and self.original_usage_verified(store_id(),identity)}

    def get(self,identity,_blocked=None):
        store=store_id();file_id(identity)
        if identity in (self.deletion_records() if _blocked is None else _blocked):
            raise DocumentError('not_found',404)
        record=self.client.call('GET',f'/vector_stores/{store}/files/{identity}')
        info=self.client.call('GET',f'/files/{identity}')
        return self.view(identity,record,info.get('filename','document'))

    def listing(self):
        store=store_id();items=[];after=None;blocked=self.deletion_records()
        for _ in range(3):
            params={'limit':100}
            if after:params['after']=after
            page=self.client.call('GET',f'/vector_stores/{store}/files',params=params)
            records=page.get('data')
            if not isinstance(records,list):raise DocumentError('invalid_upstream_response')
            for record in records:
                if not isinstance(record,dict):raise DocumentError('invalid_upstream_response')
                identity=file_id(record.get('id'))
                if identity in blocked:continue
                info=self.client.call('GET',f'/files/{identity}')
                items.append(self.view(identity,record,info.get('filename','document')))
            if not page.get('has_more'):break
            after=file_id(page.get('last_id'))
        else:raise DocumentError('too_many_documents',409)
        # Include unattached uploads so partial registration failure is recoverable.
        for identity,record in self.owned.items():
            if record['store']==store and not record['attached'] and identity not in blocked and identity not in {item['id'] for item in items}:
                items.append({'id':identity,'filename':record['name'],'status':'failed','selectable':False,'can_delete_original':self.original_usage_verified(store,identity)})
        return {'documents':items,'deletions':[{'id':identity,**record} for identity,record in blocked.items()]}

    def ensure_selected(self,identities):
        with self.selection_guard(identities):pass

    def delete(self,identity,mode,confirmation):
        store=store_id();file_id(identity)
        if not isinstance(mode,str) or mode not in {'detach','delete_original'}:raise DocumentError('invalid_delete_mode',422)
        with self.lock:
            operation=self.deletion_records().get(identity)
            if operation:
                if operation['mode']!=mode or operation['name']!=confirmation or operation['store']!=store:
                    raise DocumentError('delete_conflict',409)
            else:
                owner=self.owned.get(identity)
                if mode=='delete_original' and (not owner or owner['store']!=store):
                    raise DocumentError('original_delete_not_owned',403)
                if mode=='delete_original' and not self.original_usage_verified(store,identity):
                    raise DocumentError('original_usage_unverified',403)
                name=owner['name'] if owner else self.get(identity)['filename']
                if confirmation!=name:raise DocumentError('confirmation_mismatch',422)
                operation={'mode':mode,'name':name,'store':store,'state':'deleting','started_at':time.time(),
                           'detached':False,'original_deleted':False}
                # Persist the tombstone before the first external DELETE.
                with self.transaction() as connection:
                    existing=self.state.read(connection,store,identity).get(identity)
                    if existing:raise DocumentError('delete_conflict',409)
                    self.state.write(connection,store,identity,operation)
            return self.resume_delete(identity,mode,confirmation)

    def resume_delete(self,identity,mode,confirmation):
        with self.lock:return self._resume_delete(identity,mode,confirmation)

    def _resume_delete(self,identity,mode,confirmation):
        store=store_id();file_id(identity)
        with self.lock, self.transaction() as connection:
            operation=self.state.read(connection,store,identity).get(identity)
            if not operation:raise DocumentError('delete_not_started',404)
            if operation['mode']!=mode or operation['name']!=confirmation:raise DocumentError('delete_conflict',409)
            if operation['state']=='complete':return {'id':identity,**operation}
        # Each intent is committed before the network request. A crash after intent
        # is reconciled by GET on explicit resume, never by automatic DELETE replay.
        for kind,path,flag in [('attachment',f'/vector_stores/{store}/files/{identity}','detached'),
                               ('original',f'/files/{identity}','original_deleted')]:
            if kind=='original' and mode!='delete_original':break
            if operation[flag]:continue
            intent=kind+'_delete_sent'
            if operation.get(intent) and not operation.get(kind+'_delete_accepted'):
                # Explicit resume only: reconcile an interrupted/unacknowledged
                # request before deciding whether another DELETE is necessary.
                try:self.client.call('GET',path)
                except DocumentError as exc:
                    if exc.status!=404:raise
                    operation[flag]=True;operation[kind+'_last_http']=404
                    with self.transaction() as connection:self.state.write(connection,store,identity,operation)
                    continue
                operation[intent]=False
                with self.transaction() as connection:self.state.write(connection,store,identity,operation)
            with self.transaction() as connection:
                current=self.state.read(connection,store,identity)[identity]
                send=not current.get(intent)
                if send:
                    current[intent]=True;current['state']='deleting'
                    self.state.write(connection,store,identity,current)
                operation=current
            try:
                if send:
                    try:result=self.client.call('DELETE',path)
                    except DocumentError as exc:
                        if exc.status!=404:raise
                        result={'already_absent':True}
                    if not isinstance(result,dict):raise DocumentError('invalid_upstream_response')
                    operation[kind+'_delete_404']=result.get('already_absent') is True
                    if result.get('already_absent') is not True and result.get('deleted') is not True:raise DocumentError('delete_not_accepted',502)
                    operation[kind+'_delete_accepted']=result.get('deleted') is True
                    with self.transaction() as connection:self.state.write(connection,store,identity,operation)
                operation[flag]=self.confirm_absent(path,operation,kind)
                if not operation[flag]:operation['state']='unconfirmed'
            except DocumentError as exc:
                if exc.status==404:
                    # A DELETE 404 still needs a subsequent GET confirmation.
                    operation['state']='unconfirmed'
                else:
                    operation['state']='unconfirmed';operation['error']=exc.category
                    with self.transaction() as connection:self.state.write(connection,store,identity,operation)
                    if kind=='original':raise DocumentError('detached_original_delete_failed',exc.status,identity) from None
                    raise
            with self.transaction() as connection:self.state.write(connection,store,identity,operation)
            if not operation[flag]:return {'id':identity,**operation}
        operation['state']='complete';operation.pop('error',None)
        with self.transaction() as connection:self.state.write(connection,store,identity,operation)
        return {'id':identity,**operation}


manager=DocumentManager(OpenAIFilesClient())
