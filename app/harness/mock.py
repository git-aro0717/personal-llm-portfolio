"""Deterministic offline adapter; never calls AWS."""


def generate_response(message: str) -> str:
    return f"Mock: {message}"
