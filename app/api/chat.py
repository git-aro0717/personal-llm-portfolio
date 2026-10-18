"""Chat HTTP endpoint and request validation."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from app.conversation import ChatRequest
from app.file_search import Citation, SearchAnswer, StoreForbidden, SearchUnsupported

from app.harness import core
from app.harness.errors import InferenceError

router = APIRouter()


class ChatResponse(BaseModel):
    response: str
    sources: list[Citation] | None = None


@router.post("/chat", response_model=ChatResponse, response_model_exclude_none=True)
def chat(request: ChatRequest) -> ChatResponse:
    try:
        if request.document_ids and request.vector_store_id is None:
            raise HTTPException(status_code=422, detail="Selected documents require a vector store")
        if request.vector_store_id is not None:
            if not request.document_ids:
                from app.file_search import authorize_store
                authorize_store(request.vector_store_id)
                raise HTTPException(status_code=422,detail='Explicit document IDs required')
            options = {"history": request.history, "vector_store_id": request.vector_store_id}
            if request.document_ids:
                options["document_ids"] = request.document_ids
            result = core.process_message(request.message, **options)
        else:
            result = (core.process_message(request.message, history=request.history)
                      if request.history else core.process_message(request.message))
        return ChatResponse(**result.model_dump()) if isinstance(result, SearchAnswer) else ChatResponse(response=result)
    except StoreForbidden:
        raise HTTPException(status_code=403, detail="Vector store is not allowed") from None
    except SearchUnsupported:
        raise HTTPException(status_code=422, detail="File Search requires OpenAI backend") from None
    except InferenceError as exc:
        raise HTTPException(
            status_code=502, detail="Model service unavailable. Please try again."
        ) from exc
