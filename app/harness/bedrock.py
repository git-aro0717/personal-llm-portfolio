"""Bedrock transport using the standard AWS credential provider chain."""

import logging
import os
import re

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from app.config import ConfigurationError, get_bedrock_model_id
from app.harness.errors import InferenceError
from app.conversation import HistoryMessage

REGION = "ap-southeast-2"
logger = logging.getLogger(__name__)


def _log_failure(exc: Exception, client=None) -> None:
    """Log only sanitized exception text, never a traceback or request body."""
    message = str(exc)
    secrets = [os.environ.get(name) for name in (
        "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
        "AWS_SECURITY_TOKEN", "AWS_BEARER_TOKEN_BEDROCK",
    )]
    # Use the credentials already attached to the failing client. Do not
    # trigger another credential lookup/refresh while handling an error.
    signer = getattr(client, "_request_signer", None)
    credentials = getattr(signer, "_credentials", None)
    if credentials is not None:
        values = getattr(credentials, "__dict__", {})
        if isinstance(values, dict):
            secrets.extend(values.get(name) for name in (
                "_access_key", "_secret_key", "_token",
                "access_key", "secret_key", "token",
            ))
    for secret in sorted(
        {value for value in secrets if isinstance(value, str) and value},
        key=len, reverse=True,
    ):
        message = message.replace(secret, "[REDACTED]")
    message = re.sub(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b", "[REDACTED]", message)
    message = re.sub(
        r"(?i)([\"']?(?:aws_)?(?:access_key_id|secret_access_key|session_token|"
        r"security_token|accesskeyid|secretaccesskey|sessiontoken|"
        r"x-amz-security-token|x-amz-credential|x-amz-signature|authorization)"
        r"[\"']?\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;&]+)",
        r"\1[REDACTED]", message,
    )
    # Keep each event on one terminal line to avoid forged log entries.
    message = " ".join(message.splitlines())
    logger.error("Bedrock failure (%s): %s", type(exc).__name__, message)


class BedrockError(InferenceError):
    """An upstream inference failure, independent of HTTP."""


def generate_response(message: str, history: list[HistoryMessage] | None = None) -> str:
    """Send one user message; no credentials or clients are created at import."""
    client = None
    try:
        model_id = get_bedrock_model_id()
        client = boto3.client(
            "bedrock-runtime",
            region_name=REGION,
            config=Config(
                connect_timeout=10,
                read_timeout=120,
                # One attempt fits within the local bridge's 150-second wait
                # and avoids duplicate inference after ambiguous timeouts.
                retries={"mode": "standard", "total_max_attempts": 1},
            ),
        )
        result = client.converse(
            modelId=model_id,
            messages=[{"role": item.role, "content": [{"text": item.content}]}
                      for item in (history or [])]
                     + [{"role": "user", "content": [{"text": message}]}],
        )
    except (BotoCoreError, ClientError, ConfigurationError) as exc:
        _log_failure(exc, client)
        raise BedrockError("Bedrock request failed") from exc

    try:
        content = result["output"]["message"]["content"]
        text = "".join(block["text"] for block in content if "text" in block)
        if not text.strip():
            raise ValueError("No text in response")
        return text
    except (KeyError, TypeError, ValueError) as exc:
        _log_failure(exc, client)
        raise BedrockError("Bedrock returned an invalid response") from exc
