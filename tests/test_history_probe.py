"""Offline verification of the paid-probe script; never sends network traffic."""
import contextlib
import io
import json
import os
import re
import sys
from unittest.mock import patch

import httpx
import pytest
from deploy import verify_openai_history as probe


@pytest.mark.parametrize('scenario,expected_count,expected_code',[
    ('success',2,0),('first_failure',1,1),('second_failure',2,1),('wrong_answer',2,1)])
def test_history_probe_offline(monkeypatch,tmp_path,scenario,expected_count,expected_code):
    monkeypatch.setattr(probe,'ROOT',tmp_path)
    monkeypatch.setattr(sys,'argv',['probe','--execute'])
    seen=[]
    def respond(request):
        body=json.loads(request.content);seen.append(body)
        assert str(request.url)==probe.ENDPOINT and body['model']==probe.MODEL
        if (scenario=='first_failure' and len(seen)==1) or (scenario=='second_failure' and len(seen)==2):
            return httpx.Response(429,json={'error':{'code':'insufficient_quota','message':'private'}})
        if len(seen)==1:
            assert isinstance(body['input'],str)
            answer='了解'
        else:
            turns=body['input'];assert [turn['role'] for turn in turns]==['user','assistant','user']
            identifier=re.search(r'HIST-[A-F0-9]{24}',turns[0]['content']).group(0)
            assert identifier not in turns[2]['content']
            assert turns[1]['content']=='了解'
            answer=identifier if scenario!='wrong_answer' else 'unknown'
        return httpx.Response(200,json={'status':'completed','model':probe.MODEL,
            'usage':{'input_tokens':30,'output_tokens':5,'total_tokens':35},
            'output':[{'type':'message','content':[{'type':'output_text','text':answer}]}]})
    monkeypatch.setattr(httpx,'HTTPTransport',lambda **kwargs:httpx.MockTransport(respond))
    with patch.dict(os.environ,{'OPENAI_API_KEY':'fake-offline-key'}):
        os.environ.pop('AWS_LAMBDA_FUNCTION_NAME',None)
        output=io.StringIO()
        with contextlib.redirect_stdout(output):
            assert probe.main()==expected_code
            assert probe.main()==2  # No second paid run, even after failure.
    assert len(seen)==expected_count
    record=json.loads((tmp_path/'.aws-sam/v064-e2e/history-2turn/attempt.json').read_text())
    assert record['requests']==expected_count
    assert record['turns'][0]['upstream_http_status'] in (200,429)
    assert 'fake-offline-key' not in output.getvalue() and 'private' not in output.getvalue()
    if scenario=='success':
        assert record['turns'][1]['identifier_matches']
        assert record['turns'][1]['usage']['total_tokens']==35
