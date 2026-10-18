"""Message processing, independent of HTTP."""

from app.harness import bedrock, mock, openai_api
from app.config import get_harness_backend
from app.conversation import HistoryMessage
from app.file_search import authorize_store, SearchAnswer, SearchUnsupported


def process_message(message: str, history: list[HistoryMessage] | None = None,
                    vector_store_id: str | None = None, document_ids: list[str] | None = None) -> str | SearchAnswer:
    """Use an explicitly selected backend; never hide AWS failures."""
    backend = get_harness_backend()
    if vector_store_id is not None:
        authorize_store(vector_store_id)
        if not document_ids:raise SearchUnsupported('Explicit document IDs required')
        if backend == "bedrock":
            raise SearchUnsupported("File Search requires OpenAI backend")
        if backend == "mock":
            return SearchAnswer(response=mock.generate_response(message))
        options = {"history":history,"vector_store_id":vector_store_id}
        if document_ids:options["document_ids"]=document_ids
        return openai_api.generate_response(message, **options)
    if backend == "mock":
        return mock.generate_response(message)
    if backend == "openai":
        return (openai_api.generate_response(message, history=history) if history
                else openai_api.generate_response(message))
    return (bedrock.generate_response(message, history=history) if history
            else bedrock.generate_response(message))
