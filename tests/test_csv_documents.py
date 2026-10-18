import json
from pathlib import Path

import pytest

from local_web import csv_documents as csvdoc, documents as docs, server
from local_web.deletion_store import StateUnavailable
from test_documents import setup, upload  # Reuse the existing mocked document API.


@pytest.mark.parametrize('encoding', ['utf-8', 'utf-8-sig'])
def test_japanese_quotes_multiline_and_row_locations(encoding):
    source = '商品名,説明,在庫\r\n"ライト,試作","引用""あり\r\n２行目",0037\r\nポンプ,,82\r\n'
    text = csvdoc.csv_to_text('在庫.csv', source.encode(encoding)).decode()
    assert 'Header record 1 (source lines 1-1): ["商品名", "説明", "在庫"]' in text
    assert 'Data row 1 (CSV record 2; source lines 2-3):' in text
    assert 'Data row 2 (CSV record 3; source lines 4-4):' in text
    assert 'Column 1 "商品名": "ライト,試作"' in text
    assert json.dumps('引用"あり\r\n２行目', ensure_ascii=False) in text
    assert 'Column 3 "在庫": "0037"' in text
    assert 'Column 2 "説明": ""' in text
    assert '\ufeff' not in text


@pytest.mark.parametrize('source', [
    '', '\ufeff', 'a,b\n', 'a,b\n1\n', 'a,b\n1,2,3\n',
    ',b\n1,2\n', 'a,a\n1,2\n', 'a, a \n1,2\n',
    'a,b\n"unterminated,2\n', 'a,b\nstray"quote,2\n',
    'a,b\n"closed"junk,2\n', 'a,b\n\n', 'a,b\n1,\x00\n',
])
def test_invalid_csv_rejected_before_remote_and_db(setup, source, monkeypatch):
    client, manager, fake = setup
    def no_db():
        raise AssertionError('Invalid CSV must not access SQLite')
    monkeypatch.setattr(manager, 'deletion_records', no_db)
    response = upload(client, 'bad.csv', source.encode('utf-8'))
    assert response.status_code in (413, 422)
    assert not fake.calls


def test_non_utf8_and_oversized_csv(setup):
    client, _, fake = setup
    assert upload(client, 'bad.csv', b'a\n\xff').status_code == 422
    assert upload(client, 'large.csv', b'x' * (docs.MAX_FILE_BYTES + 1)).status_code == 413
    assert not fake.calls


@pytest.mark.parametrize('source,category', [
    ('a\n' + 'x\n' * (csvdoc.MAX_ROWS + 1), 'csv_too_many_rows'),
    (','.join('h'+str(i) for i in range(csvdoc.MAX_COLUMNS + 1)) + '\n' + ','.join('x' for _ in range(csvdoc.MAX_COLUMNS + 1)), 'csv_too_many_columns'),
    ('a\n' + 'x' * (csvdoc.MAX_CELL_CHARS + 1), 'csv_cell_too_large'),
    ('h' * (csvdoc.MAX_CELL_CHARS + 1) + '\nx', 'csv_cell_too_large'),
])
def test_structural_limits(setup, source, category):
    client, _, fake = setup
    response = upload(client, 'large.csv', source.encode())
    assert response.status_code == 413 and response.json()['detail'] == category
    assert not fake.calls


def test_valid_limits_and_literal_cells():
    source = ','.join('h'+str(i) for i in range(csvdoc.MAX_COLUMNS)) + '\n' + ','.join('x' for _ in range(csvdoc.MAX_COLUMNS))
    assert 'Column 50 "h49": "x"' in csvdoc.csv_to_text('a.csv', source.encode()).decode()
    assert 'Data row 5000 ' in csvdoc.csv_to_text('a.csv', ('a\n' + 'x\n' * csvdoc.MAX_ROWS).encode()).decode()
    assert json.dumps('x' * csvdoc.MAX_CELL_CHARS) in csvdoc.csv_to_text('a.csv', ('a\n' + 'x' * csvdoc.MAX_CELL_CHARS).encode()).decode()
    text = csvdoc.csv_to_text('a.csv', b'a,b\n=1+2,0001').decode()
    assert '"=1+2"' in text and '"0001"' in text  # Never evaluate spreadsheet formulas.


def test_converted_size_checked_before_remote(setup):
    client, _, fake = setup
    # Repeated column names expand despite a very small CSV input.
    source = 'h' * 4000 + '\n' + 'x\n' * 2000
    response = upload(client, 'expanded.csv', source.encode())
    assert response.status_code == 413
    assert response.json()['detail'] == 'converted_csv_too_large'
    assert not fake.calls


@pytest.mark.parametrize('mode', ['detach', 'delete_original'])
def test_csv_upload_list_select_delete_and_restart_tombstone(setup, monkeypatch, mode):
    client, manager, fake = setup
    response = upload(client, '在庫.CSV', '商品名,在庫\n架空ライト,37'.encode())
    assert response.status_code == 202
    identity = response.json()['id']
    assert response.json()['filename'] == '在庫.CSV.txt'
    post = next(kwargs for method, path, kwargs in fake.calls if method == 'POST' and path == '/files')
    remote_name, remote_bytes = post['files']['file']
    assert remote_name == '在庫.CSV.txt'
    assert 'Column 2 "在庫": "37"' in remote_bytes.decode()
    assert fake.records[identity]['attributes'] == {docs.ATTRIBUTE: identity}
    fake.records[identity]['status'] = 'completed'
    assert client.get('/api/documents').json()['documents'][0]['filename'] == remote_name
    seen = []
    monkeypatch.setattr(server, 'forward_message', lambda *args: (seen.append(args) or (200, {'response':'Mock answer','sources':[{'file_id':identity,'filename':remote_name}]})))
    answer = client.post('/api/chat', json={'message':'在庫は？','document_ids':[identity]})
    assert answer.status_code == 200 and answer.json()['sources'][0]['filename'] == remote_name
    assert seen[0][2:] == ('vs_allowed', [identity])
    deletion = client.post(f'/api/documents/{identity}/delete', json={'mode':mode,'confirmation':remote_name})
    assert deletion.status_code == 200 and deletion.json()['state'] == 'complete'
    assert deletion.json()['original_deleted'] == (mode == 'delete_original')
    assert (identity in fake.files) == (mode == 'detach')
    assert client.get('/api/documents').json()['documents'] == []
    assert client.post('/api/chat', json={'message':'再検索','document_ids':[identity]}).status_code == 404
    restarted = docs.DocumentManager(fake, state=manager.state)
    with pytest.raises(docs.DocumentError) as exc:
        restarted.ensure_selected([identity])
    assert exc.value.status == 404 and len(seen) == 1


def test_csv_partial_registration_and_db_failure(setup, monkeypatch):
    client, manager, fake = setup
    def unavailable():
        raise StateUnavailable()
    monkeypatch.setattr(manager.state, 'location', unavailable)
    assert upload(client, 'a.csv', b'a\nx').status_code == 503
    assert not fake.calls


def test_csv_registration_failure_keeps_consistent_remote_name(setup):
    client, _, fake = setup
    fake.fail = '/vector_stores/vs_allowed/files'
    response = upload(client, 'a.csv', b'a\nx')
    assert response.status_code == 502
    fake.fail = None
    item = client.get('/api/documents').json()['documents'][0]
    assert item['filename'] == 'a.csv.txt' and item['status'] == 'failed'
    assert client.post(f"/api/documents/{item['id']}/delete", json={'mode':'delete_original','confirmation':item['filename']}).status_code == 200


@pytest.mark.parametrize('header,value', [('host','evil.example'), ('origin','https://evil.example'), ('x-csrf-token','wrong')])
def test_csv_preserves_request_security(setup, header, value):
    client, _, fake = setup
    client.headers[header] = value
    assert upload(client, 'a.csv', b'a\nx').status_code == 403
    assert not fake.calls


def test_csv_forbidden_store_before_upload(setup, monkeypatch):
    monkeypatch.setenv('OPENAI_DOCUMENT_VECTOR_STORE_ID', 'vs_forbidden')
    assert upload(setup[0], 'a.csv', b'a\nx').status_code == 403
    assert not setup[2].calls


def test_fictional_csv_fixture():
    path = Path(__file__).parents[1] / 'docs/e2e/v1.1/inventory-fictional.csv'
    text = csvdoc.csv_to_text(path.name, path.read_bytes()).decode()
    assert 'CSV-ALPHA-731' in text and 'CSV-BETA-842' in text
    assert '"37"' in text and '"82"' in text


@pytest.mark.parametrize('length,expected', [(176, 202), (177, 422)])
def test_csv_name_limit_preserves_delete_confirmation(setup, length, expected):
    client, _, fake = setup
    name = 'x' * (length - 4) + '.csv'
    response = upload(client, name, b'a\nx')
    assert response.status_code == expected
    if expected == 202:
        assert response.json()['filename'] == name + '.txt'
        identity = response.json()['id']
        deletion = client.post(f'/api/documents/{identity}/delete', json={'mode':'detach','confirmation':response.json()['filename']})
        assert deletion.status_code == 200 and deletion.json()['state'] == 'complete'
    else:
        assert not fake.calls
