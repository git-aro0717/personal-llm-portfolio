"""Serve a private localhost UI and sign requests to one fixed Lambda URL."""

import json
import secrets
from pathlib import Path

import boto3
import httpx
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.exceptions import BotoCoreError
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import ValidationError
from app.conversation import ChatRequest, HistoryMessage
from app.file_search import authorize_store, StoreForbidden, SearchAnswer
from local_web import documents

HOST = "127.0.0.1:8765"
ORIGIN = f"http://{HOST}"
TARGET = "https://example.invalid/chat"
MAX_BODY = 16 * 1024
MAX_RESPONSE = 1024 * 1024
TOKEN = secrets.token_urlsafe(32)
STATIC = Path(__file__).parent / "static"
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def protect_local_server(request: Request, call_next):
    if (request.headers.get("host") != HOST
            or request.client is None
            or request.client.host not in {"127.0.0.1", "::1"}):
        return JSONResponse({"detail": "Invalid local host"}, status_code=403)
    origin = request.headers.get("origin")
    if origin is not None and origin != ORIGIN:
        return JSONResponse({"detail": "Invalid origin"}, status_code=403)
    if request.method == "POST":
        if origin != ORIGIN or not all(
            secrets.compare_digest(value, TOKEN) for value in (
                request.cookies.get("local_session", ""),
                request.headers.get("x-csrf-token", ""),
            )
        ):
            return JSONResponse({"detail": "Invalid CSRF token"}, status_code=403)
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
    )
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/ui.js")
def javascript():
    return FileResponse(STATIC / "ui.js", media_type="application/javascript")


@app.get("/style.css")
def stylesheet():
    return FileResponse(STATIC / "style.css", media_type="text/css")


@app.get("/api/session")
def session():
    response = JSONResponse({"csrf": TOKEN})
    response.set_cookie("local_session", TOKEN, httponly=True, samesite="strict")
    return response


def forward_message(message: str, history: list[HistoryMessage] | None = None,
                    vector_store_id: str | None = None, document_ids: list[str] | None = None) -> tuple[int, dict]:
    """Credentials stay in this process; redirects and arbitrary URLs are forbidden."""
    if vector_store_id is not None:
        authorize_store(vector_store_id)
        if not document_ids:return 422,{'detail':'Explicit document IDs required'}
    credentials = boto3.Session().get_credentials()
    if credentials is None:
        return 403, {"detail": "AWS credentials unavailable"}
    payload = {"message": message}
    if history:
        payload["history"] = [item.model_dump() for item in history]
    if vector_store_id is not None:
        payload["vector_store_id"] = vector_store_id
    if document_ids:
        payload['document_ids']=document_ids
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(body) > MAX_BODY:
        return 413, {"detail": "Input exceeds 16 KiB"}
    signed = AWSRequest(method="POST", url=TARGET, data=body,
                        headers={"Content-Type": "application/json"})
    SigV4Auth(credentials.get_frozen_credentials(), "lambda", "ap-southeast-2").add_auth(signed)
    with httpx.Client(timeout=httpx.Timeout(150, connect=10),
                      follow_redirects=False, trust_env=False) as client:
        with client.stream("POST", TARGET, content=body, headers=dict(signed.headers)) as response:
            data = bytearray()
            for chunk in response.iter_bytes():
                data.extend(chunk)
                if len(data) > MAX_RESPONSE:
                    return 502, {"detail": "Invalid model service response"}
            if response.status_code in {422, 403, 502}:
                return response.status_code, {"detail": "Upstream request rejected"}
            if response.status_code != 200:
                return 502, {"detail": "Model service unavailable"}
            try:
                result = json.loads(data)
                if not isinstance(result, dict) or not isinstance(result.get("response"), str):
                    raise ValueError()
                if vector_store_id is not None:
                    return 200, SearchAnswer.model_validate(result).model_dump()
            except (ValueError, TypeError):
                return 502, {"detail": "Invalid model service response"}
            return 200, {"response": result["response"]}


@app.post("/api/chat")
async def chat(request: Request):
    if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
        return JSONResponse({"detail": "JSON required"}, status_code=422)
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > MAX_BODY:
            return JSONResponse({"detail": "Input exceeds 16 KiB"}, status_code=413)
    try:
        payload = json.loads(data)
        validated = ChatRequest.model_validate(payload)
    except (ValueError, UnicodeError, ValidationError):
        return JSONResponse({"detail": "Invalid message or history"}, status_code=422)
    # Keep blocking signing/HTTP work off the ASGI event loop.
    from starlette.concurrency import run_in_threadpool
    try:
        if validated.document_ids:
            selected_store=documents.store_id()
            if validated.vector_store_id is not None and validated.vector_store_id!=selected_store:
                raise StoreForbidden()
            def selected_chat():
                with documents.manager.selection_guard(validated.document_ids):
                    return forward_message(validated.message,validated.history,selected_store,validated.document_ids)
            status,body=await run_in_threadpool(selected_chat)
        elif validated.vector_store_id is not None:
            return JSONResponse({'detail':'Explicit document IDs required'},status_code=422)
        elif validated.history:
            status, body = await run_in_threadpool(forward_message, validated.message, validated.history)
        else:
            status, body = await run_in_threadpool(forward_message, validated.message)
    except StoreForbidden:
        status, body = 403, {"detail": "Vector store is not allowed"}
    except BotoCoreError:
        status, body = 403, {"detail": "AWS credentials unavailable or expired"}
    except httpx.TimeoutException:
        status, body = 504, {"detail": "Upstream timeout"}
    except httpx.RequestError:
        status, body = 503, {"detail": "Network connection failed"}
    return JSONResponse(body, status_code=status)


@app.exception_handler(documents.DocumentError)
async def document_error(request, exc):
    body={'detail':exc.category}
    if exc.file_id:body['file_id']=exc.file_id
    return JSONResponse(body,status_code=exc.status)


@app.exception_handler(StoreForbidden)
async def store_error(request, exc):
    return JSONResponse({'detail':'Vector store is not allowed'},status_code=403)


@app.get('/api/documents')
def list_documents():
    return documents.manager.listing()


@app.get('/api/documents/{identity}')
def document_status(identity: str):
    return documents.manager.get(identity)


@app.post('/api/documents')
async def upload_document(request: Request, filename: str):
    documents.store_id()  # Refuse before buffering or fetching credentials.
    if request.headers.get('content-type','').split(';')[0]!='application/octet-stream':
        return JSONResponse({'detail':'Binary body required'},status_code=422)
    data=bytearray()
    async for chunk in request.stream():
        if len(data)+len(chunk)>documents.MAX_FILE_BYTES:
            return JSONResponse({'detail':'File exceeds 5 MiB'},status_code=413)
        data.extend(chunk)
    documents.validate_file(filename,data)
    from starlette.concurrency import run_in_threadpool
    result=await run_in_threadpool(documents.manager.upload,filename,bytes(data))
    return JSONResponse(result,status_code=202)


@app.post('/api/documents/{identity}/delete')
async def delete_document(identity: str, request: Request):
    data=bytearray()
    async for chunk in request.stream():
        if len(data)+len(chunk)>2048:return JSONResponse({'detail':'Delete request too large'},status_code=413)
        data.extend(chunk)
    try:
        body=json.loads(data)
        if not isinstance(body,dict) or set(body)!={'mode','confirmation'} or not isinstance(body['confirmation'],str):raise ValueError()
    except (ValueError,UnicodeError):return JSONResponse({'detail':'Invalid confirmation'},status_code=422)
    from starlette.concurrency import run_in_threadpool
    return await run_in_threadpool(documents.manager.delete,identity,body['mode'],body['confirmation'])


@app.get('/documents.js')
def documents_javascript():
    return FileResponse(STATIC/'documents.js',media_type='application/javascript')


def main():
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8765, access_log=False, proxy_headers=False)


if __name__ == "__main__":
    main()
