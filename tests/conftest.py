import pytest


@pytest.fixture(autouse=True)
def default_backend(monkeypatch,tmp_path):
    # Developer shell settings must not change the test suite's behavior.
    monkeypatch.setenv("HARNESS_BACKEND", "bedrock")
    monkeypatch.setenv('PERSONAL_LLM_DOCUMENT_DB',str(tmp_path/'documents.sqlite3'))
