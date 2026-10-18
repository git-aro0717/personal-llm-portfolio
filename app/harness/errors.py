"""Provider-independent inference failure contract."""


class InferenceError(Exception):
    """The API layer returns a generic 502 for inference failures."""
