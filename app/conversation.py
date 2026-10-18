"""Shared, provider-independent chat input contract."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator
from app.file_search import STORE_PATTERN

MAX_MESSAGE_CHARS = 4000
MAX_HISTORY = 20
MAX_REQUEST_BYTES = 16 * 1024


class HistoryMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["user", "assistant"]
    content: StrictStr = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)

    @field_validator("content")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("content must not be blank")
        return value


class ChatRequest(BaseModel):
    message: StrictStr = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)
    history: list[HistoryMessage] = Field(default_factory=list, max_length=MAX_HISTORY, strict=True)
    vector_store_id: StrictStr | None = Field(default=None, pattern=STORE_PATTERN)
    document_ids: list[StrictStr] = Field(default_factory=list, max_length=20, strict=True)

    @field_validator("document_ids")
    @classmethod
    def valid_documents(cls, values):
        import re
        if len(set(values)) != len(values) or any(not re.fullmatch(r"file[-_][A-Za-z0-9_-]{1,128}", value) for value in values):
            raise ValueError("invalid document IDs")
        return values

    @field_validator("message")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("message must not be blank")
        return value


class ChatBodyLimit:
    """Bound raw /chat input before JSON parsing, including chunked bodies."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST" or scope["path"] != "/chat":
            return await self.app(scope, receive, send)
        body = bytearray()
        while True:
            event = await receive()
            if event["type"] == "http.disconnect":
                return
            chunk = event.get("body", b"")
            if len(body) + len(chunk) > MAX_REQUEST_BYTES:
                from starlette.responses import JSONResponse
                return await JSONResponse({"detail": "Input exceeds 16 KiB"}, status_code=413)(scope, receive, send)
            body.extend(chunk)
            if not event.get("more_body", False):
                break
        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)
