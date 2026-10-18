"""FastAPI application entry point."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.chat import router as chat_router
from app.conversation import ChatBodyLimit
from app.config import get_bedrock_model_id, get_harness_backend
from app.harness.openai_api import get_settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger = logging.getLogger("uvicorn.error")
    backend = get_harness_backend()
    if backend == "bedrock":
        logger.info("Bedrock model ID: %s", get_bedrock_model_id())
    elif backend == "openai":
        get_settings()  # Validate non-secret settings; retrieve keys only on use.
        logger.info("Harness backend: openai (Responses API)")
    else:
        logger.info("Harness backend: mock (no AWS calls)")
    yield

app = FastAPI(title="Personal LLM System", version="0.1.0", lifespan=lifespan)
app.include_router(chat_router)
app.add_middleware(ChatBodyLimit)
