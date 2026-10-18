"""Environment configuration shared by startup and the harness."""

import os
import re


class ConfigurationError(ValueError):
    """Missing or invalid application configuration."""


def get_harness_backend() -> str:
    backend = os.environ.get("HARNESS_BACKEND", "mock").strip().lower()
    if backend not in {"bedrock", "mock", "openai"}:
        raise ConfigurationError("HARNESS_BACKEND must be bedrock, mock, or openai")
    return backend


def get_bedrock_model_id() -> str:
    """Read the current environment; never fall back to a built-in model."""
    value = os.environ.get("BEDROCK_MODEL_ID", "").strip()
    if not value:
        raise ConfigurationError("BEDROCK_MODEL_ID must be set before starting Uvicorn")
    # Only accept model/profile identifiers, not arbitrary log text or secrets.
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*(?::[0-9]+)?", value):
        raise ConfigurationError("BEDROCK_MODEL_ID is not a valid model/profile ID")
    if value in {os.environ.get(name) for name in (
        "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
        "AWS_SECURITY_TOKEN", "AWS_BEARER_TOKEN_BEDROCK",
    )}:
        raise ConfigurationError("BEDROCK_MODEL_ID must not contain AWS credentials")
    return value
