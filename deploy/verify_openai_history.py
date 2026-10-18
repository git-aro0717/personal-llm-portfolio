"""Two-turn local Backend history probe. Preview by default; no retries or AWS."""
import argparse
import json
import logging
import os
from pathlib import Path
import secrets
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
MODEL = 'gpt-5.6-luna'
ENDPOINT = 'https://api.openai.com/v1/responses'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    if not args.execute:
        print(json.dumps({'mode':'preview','endpoint':ENDPOINT,'model':MODEL,'max_requests':2,
                          'test':'Generate fictional identifier; second question receives it only through history'}))
        return 0
    key = os.environ.get('OPENAI_API_KEY','')
    if not key.strip() or any(c.isspace() for c in key) or os.environ.get('AWS_LAMBDA_FUNCTION_NAME'):
        print('{"status":"stopped","category":"invalid_local_credentials_or_environment","requests":0}')
        return 2
    del key
    # These settings affect only this child process, never the parent or AWS.
    os.environ.update(HARNESS_BACKEND='openai',OPENAI_MODEL_ID=MODEL,
                      OPENAI_MAX_OUTPUT_TOKENS='512',OPENAI_TIMEOUT_SECONDS='60')
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('httpcore').setLevel(logging.WARNING)
    import httpx
    from fastapi.testclient import TestClient
    from app.main import app
    from app.harness import openai_api

    marker = ROOT/'.aws-sam/v064-e2e/history-2turn/attempt.json'
    marker.parent.mkdir(parents=True,exist_ok=True)
    result = {'requested_model':MODEL,'status':'started','requests':0,'turns':[]}
    try:
        with marker.open('x',encoding='utf-8') as output:json.dump(result,output)
    except FileExistsError:
        print('{"status":"stopped","category":"attempt_already_recorded","requests":0}')
        return 2
    identifier = 'HIST-'+secrets.token_hex(12).upper()
    first = f'この会話専用の架空識別子は{identifier}です。「了解」とだけ答えてください。'
    second = '直前に伝えた会話専用の架空識別子だけを返してください。'
    expected_input = None
    active = None
    real_client = httpx.Client

    def save():
        marker.write_text(json.dumps(result,ensure_ascii=True,indent=2),encoding='utf-8')

    class Capture(httpx.BaseTransport):
        def __init__(self):self.inner=httpx.HTTPTransport(retries=0)
        def close(self):self.inner.close()
        def handle_request(self,request):
            if result['requests']>=2 or request.method!='POST' or str(request.url)!=ENDPOINT:
                raise RuntimeError('Unexpected external operation blocked')
            payload=json.loads(request.content)
            if payload.get('model')!=MODEL or payload.get('input')!=expected_input or 'tools' in payload or payload.get('store') is not False:
                raise RuntimeError('Unexpected inference payload blocked')
            result['requests']+=1
            save()  # Record attempted call before network I/O, including ambiguous timeouts.
            request.headers['accept-encoding']='identity'
            upstream=self.inner.handle_request(request)
            try:
                active['upstream_http_status']=upstream.status_code
                data=bytearray()
                chunks=[upstream.content] if upstream.is_stream_consumed else upstream.iter_raw()
                for chunk in chunks:
                    if len(data)+len(chunk)>1024*1024:raise httpx.ReadError('Response size limit')
                    data.extend(chunk)
                try:
                    body=json.loads(data)
                    if isinstance(body,dict):
                        model=body.get('model')
                        if isinstance(model,str) and len(model)<=128:active['returned_model']=model
                        usage=body.get('usage')
                        if isinstance(usage,dict):
                            active['usage']={k:v for k,v in usage.items() if k in ('input_tokens','output_tokens','total_tokens') and type(v) is int}
                            details=usage.get('output_tokens_details')
                            if isinstance(details,dict) and type(details.get('reasoning_tokens')) is int:active['reasoning_tokens']=details['reasoning_tokens']
                        error=body.get('error')
                        if isinstance(error,dict) and error.get('code') in ('invalid_api_key','insufficient_quota','rate_limit_exceeded','model_not_found'):
                            active['upstream_error_code']=error['code']
                except (ValueError,TypeError):pass
                return httpx.Response(upstream.status_code,content=bytes(data))
            finally:upstream.close()

    class Categories(logging.Handler):
        def emit(self,record):
            text=record.getMessage();prefix='OpenAI inference failed: '
            if active is not None and text.startswith(prefix):
                category=text[len(prefix):]
                if category.isascii() and category.replace('_','').isalpha():active['category']=category

    def provider_client(**kwargs):return real_client(transport=Capture(),**kwargs)
    safe_log=Categories();openai_api.logger.addHandler(safe_log)
    started=time.monotonic()
    history=[]
    try:
        with TestClient(app,raise_server_exceptions=False) as client:
            openai_api.httpx.Client=provider_client
            for number,message in enumerate((first,second),1):
                active={'turn':number};result['turns'].append(active)
                expected_input=message if not history else history+[{'role':'user','content':message}]
                tick=time.monotonic()
                response=client.post('/chat',json={'message':message,'history':history})
                active.update(backend_http_status=response.status_code,elapsed_seconds=round(time.monotonic()-tick,3))
                if response.status_code!=200:
                    result['status']='failed';save();break
                answer=response.json().get('response')
                if not isinstance(answer,str) or not answer.strip():
                    result.update(status='failed',category='invalid_backend_response');save();break
                active['response']=answer
                if active.get('returned_model')!=MODEL:
                    result.update(status='failed',category='unexpected_model');save();break
                if number==1:
                    history=[{'role':'user','content':first},{'role':'assistant','content':answer}]
                    save()
                else:
                    active['identifier_matches']=answer.strip()==identifier
                    result['status']='success' if active['identifier_matches'] else 'failed'
                    if not active['identifier_matches']:result['category']='history_not_recalled_exactly'
    except Exception as exc:
        result.update(status='failed',category='local_probe_error',error_type=type(exc).__name__)
    finally:
        openai_api.httpx.Client=real_client
        openai_api.logger.removeHandler(safe_log)
        result['elapsed_seconds']=round(time.monotonic()-started,3)
        save()
    print(json.dumps(result,ensure_ascii=True))
    return 0 if result['status']=='success' else 1


if __name__=='__main__':raise SystemExit(main())
