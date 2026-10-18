"""Responses API adapter. Keys exist only in process memory, never in logs."""

import json
import logging
import os
import re
from dataclasses import dataclass

import boto3
import httpx
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from app.config import ConfigurationError
from app.harness.errors import InferenceError
from app.conversation import HistoryMessage
from app.file_search import authorize_store, extract_sources, SearchAnswer

ENDPOINT = "https://api.openai.com/v1/responses"
logger = logging.getLogger(__name__)


class OpenAIError(InferenceError):
    """Safe category, without upstream messages or credential values."""


@dataclass(frozen=True)
class Settings:
    model: str
    max_output_tokens: int
    timeout: int


def get_settings() -> Settings:
    model = os.environ.get("OPENAI_MODEL_ID", "").strip()
    if not model or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", model):
        raise ConfigurationError("OPENAI_MODEL_ID must be a valid model ID")
    try:
        tokens = int(os.environ.get("OPENAI_MAX_OUTPUT_TOKENS", "512"))
        timeout = int(os.environ.get("OPENAI_TIMEOUT_SECONDS", "60"))
    except ValueError:
        raise ConfigurationError("OpenAI numeric settings are invalid") from None
    if not 16 <= tokens <= 4096 or not 1 <= timeout <= 120:
        raise ConfigurationError("OpenAI limits: tokens 16..4096, timeout 1..120 seconds")
    return Settings(model, tokens, timeout)


def get_api_key() -> str:
    if os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
        # Never fall back to a plaintext Lambda environment key.
        secret_id = os.environ.get("OPENAI_SECRET_ID", "").strip()
        if not secret_id:
            raise OpenAIError("secret_configuration")
        try:
            client = boto3.client("secretsmanager", region_name="ap-southeast-2",
                                  config=Config(connect_timeout=5, read_timeout=10,
                                                retries={"total_max_attempts": 1}))
            result = client.get_secret_value(SecretId=secret_id, VersionStage="AWSCURRENT")
            payload = json.loads(result.get("SecretString", ""))
            key = payload.get("api_key") if isinstance(payload, dict) else None
        except (BotoCoreError, ClientError, ValueError, TypeError):
            raise OpenAIError("secret_unavailable") from None
    else:
        key = os.environ.get("OPENAI_API_KEY")
    if not isinstance(key, str) or not key.strip() or any(c.isspace() for c in key):
        raise OpenAIError("credentials_unavailable")
    return key


def _error_category(status: int, data: bytes) -> str:
    if status in {401, 403}:
        return "authentication"
    if status in {402, 429}:
        try:
            error = json.loads(data).get("error", {})
            codes = {error.get("code"), error.get("type")}
        except (ValueError, TypeError, AttributeError):
            codes = set()
        if codes.intersection({"insufficient_quota", "billing_hard_limit_reached",
                               "organization_spend_limit_exceeded", "project_spend_limit_exceeded",
                               "organization_usage_limit_exceeded", "billing_not_active"}):
            return "billing_or_quota"
        return "rate_limit" if status == 429 else "billing_or_quota"
    return "upstream_http"


def generate_response(message: str, history: list[HistoryMessage] | None = None,
                      vector_store_id: str | None = None, document_ids: list[str] | None = None) -> str | SearchAnswer:
    if document_ids and (vector_store_id is None or len(document_ids)>20 or
                         any(not isinstance(value,str) or not re.fullmatch(r"file[-_][A-Za-z0-9_-]{1,128}",value) for value in document_ids)):
        raise OpenAIError('invalid_document_selection')
    if vector_store_id is not None:
        authorize_store(vector_store_id)  # Reject before retrieving credentials.
        if not document_ids:raise OpenAIError('explicit_documents_required')
    try:
        settings = get_settings()
        key = get_api_key()
        payload = {"model": settings.model, "input": (
            [item.model_dump() for item in history] + [{"role": "user", "content": message}]
            if history else message), "max_output_tokens": settings.max_output_tokens, "store": False}
        if vector_store_id is not None:
            payload["tools"] = [{"type": "file_search", "vector_store_ids": [vector_store_id], "max_num_results": 5}]
            if document_ids:
                if len(document_ids)>20 or any(not re.fullmatch(r"file[-_][A-Za-z0-9_-]{1,128}", value) for value in document_ids):
                    raise OpenAIError('invalid_document_selection')
                payload['tools'][0]['filters'] = {'type':'in','key':'personal_llm_document_id','value':document_ids}
        with httpx.Client(timeout=httpx.Timeout(settings.timeout, connect=10),
                          follow_redirects=False, trust_env=False) as client:
            # Fail closed on removed/not-indexed files, including direct Lambda callers.
            for identity in document_ids or []:
                with client.stream('GET',f'https://api.openai.com/v1/vector_stores/{vector_store_id}/files/{identity}',
                                   headers={'Authorization':f'Bearer {key}'}) as check:
                    metadata=bytearray()
                    for chunk in check.iter_bytes():
                        if len(metadata)+len(chunk)>65536:raise OpenAIError('invalid_document_selection')
                        metadata.extend(chunk)
                    if check.status_code!=200:raise OpenAIError('document_unavailable')
                    try:record=json.loads(metadata)
                    except ValueError:raise OpenAIError('document_unavailable') from None
                    if not isinstance(record,dict) or record.get('status')!='completed' or not isinstance(record.get('attributes'),dict) or record['attributes'].get('personal_llm_document_id')!=identity:
                        raise OpenAIError('document_not_ready')
                with client.stream('GET',f'https://api.openai.com/v1/files/{identity}',
                                   headers={'Authorization':f'Bearer {key}'}) as original:
                    if original.status_code!=200:raise OpenAIError('document_unavailable')
            with client.stream("POST", ENDPOINT,
                               headers={"Authorization": f"Bearer {key}"},
                               json=payload) as response:
                data = bytearray()
                for chunk in response.iter_bytes():
                    if len(data) + len(chunk) > 1024 * 1024:
                        raise OpenAIError("response_too_large")
                    data.extend(chunk)
                if response.status_code != 200:
                    raise OpenAIError(_error_category(response.status_code, data))
        try:
            result = json.loads(data)
            if result.get("status") != "completed" or result.get("error"):
                raise OpenAIError("incomplete_response")
            text = "".join(
                content["text"]
                for item in result["output"] if item.get("type") == "message"
                for content in item.get("content", []) if content.get("type") == "output_text"
            )
            if not text.strip():
                raise OpenAIError("empty_response")
            return (SearchAnswer(response=text, sources=[source for source in extract_sources(result["output"])
                                                       if not document_ids or source.file_id in document_ids])
                    if vector_store_id is not None else text)
        except (ValueError, KeyError, TypeError, AttributeError):
            raise OpenAIError("invalid_response") from None
    except ConfigurationError:
        category = "configuration"
    except httpx.TimeoutException:
        category = "timeout"
    except httpx.RequestError:
        category = "network"
    except OpenAIError as exc:
        category = str(exc)  # Only fixed categories created in this module.
    # Never log exception text, response bodies, headers, keys, or request input.
    logger.error("OpenAI inference failed: %s", category)
    raise OpenAIError(category) from None
