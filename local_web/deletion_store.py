"""Local metadata only. Lazy SQLite access; never store credentials or content."""
import os
import sqlite3
import json
from pathlib import Path
from contextlib import contextmanager


class StateUnavailable(Exception):
    pass


class DeletionStore:
    def __init__(self, path=None):
        self.path = path

    def location(self):
        path = Path(self.path or os.environ.get('PERSONAL_LLM_DOCUMENT_DB', '') or
                    Path(os.environ.get('LOCALAPPDATA', Path.home()/'.local/share'))/'PersonalLLM/documents.sqlite3').resolve()
        roots = [Path(__file__).resolve().parents[1]]
        roots += [Path(v).resolve() for k,v in os.environ.items() if k.lower().startswith('onedrive') and v]
        if any(path.is_relative_to(root) for root in roots) or any(p.name.lower()=='onedrive' or (p/'.git').exists() for p in path.parents):
            raise StateUnavailable()
        return path

    @contextmanager
    def transaction(self):
        connection = None
        try:
            path = self.location();path.parent.mkdir(parents=True,exist_ok=True)
            connection = sqlite3.connect(path,timeout=5)
            connection.execute('CREATE TABLE IF NOT EXISTS deletions (store_id TEXT NOT NULL, file_id TEXT NOT NULL, state TEXT NOT NULL, metadata TEXT NOT NULL, PRIMARY KEY(store_id,file_id))')
            connection.execute('BEGIN IMMEDIATE')
            yield connection
            connection.commit()
        except (sqlite3.Error,OSError,ValueError) as exc:
            if connection:connection.rollback()
            raise StateUnavailable() from None
        finally:
            if connection:connection.close()

    def read(self,connection,store,identity=None):
        rows = connection.execute('SELECT file_id,metadata FROM deletions WHERE store_id=?'+(' AND file_id=?' if identity else ''),
                                  (store,identity) if identity else (store,)).fetchall()
        try:
            result={key:json.loads(value) for key,value in rows}
            if any(not isinstance(value,dict) or value.get('state') not in ('deleting','unconfirmed','complete')
                   or value.get('store')!=store or value.get('mode') not in ('detach','delete_original')
                   or not isinstance(value.get('name'),str) or type(value.get('detached')) is not bool
                   or type(value.get('original_deleted')) is not bool for value in result.values()):raise ValueError()
            return result
        except (ValueError,TypeError):raise StateUnavailable() from None

    def write(self,connection,store,identity,record):
        connection.execute('INSERT INTO deletions VALUES (?,?,?,?) ON CONFLICT(store_id,file_id) DO UPDATE SET state=excluded.state,metadata=excluded.metadata',
                           (store,identity,record['state'],json.dumps(record)))
