"""Local authorization and provider-neutral citation contract; no API calls."""
import os
import re
from pydantic import BaseModel, ConfigDict, Field, StrictStr

STORE_PATTERN = r"^vs_[A-Za-z0-9_-]{1,128}$"


class StoreForbidden(ValueError):
    pass


class SearchUnsupported(ValueError):
    pass


def authorize_store(store_id: str) -> None:
    # Empty/unconfigured allowlist denies all. No client-supplied allowlist.
    allowed = {value.strip() for value in os.environ.get("OPENAI_ALLOWED_VECTOR_STORE_IDS", "").split(",") if value.strip()}
    if not re.fullmatch(STORE_PATTERN, store_id) or store_id not in allowed:
        raise StoreForbidden("Vector store is not allowed")


class Citation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    file_id: StrictStr = Field(pattern=r"^file[-_][A-Za-z0-9_-]{1,128}$")
    filename: StrictStr = Field(min_length=1, max_length=255)


class SearchAnswer(BaseModel):
    response: str
    sources: list[Citation] = Field(default_factory=list, max_length=20)


def extract_sources(output: list) -> list[Citation]:
    sources = []
    seen = set()
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for block in item.get("content", []):
            if not isinstance(block, dict) or block.get("type") != "output_text":
                continue
            annotations = block.get("annotations", [])
            if not isinstance(annotations, list):
                continue
            for annotation in annotations:
                if not isinstance(annotation, dict) or annotation.get("type") != "file_citation":
                    continue
                file_id, name = annotation.get("file_id"), annotation.get("filename")
                if not isinstance(file_id, str) or not re.fullmatch(r"file[-_][A-Za-z0-9_-]{1,128}", file_id) or not isinstance(name, str):
                    continue
                # Metadata is display text, never a path, HTML or download URL.
                name = name.replace("\\", "/").rsplit("/", 1)[-1]
                name = "".join(c for c in name if c.isprintable())[:255].strip()
                if not name or file_id in seen:
                    continue
                sources.append(Citation(file_id=file_id, filename=name))
                seen.add(file_id)
                if len(sources) == 20:
                    return sources
    return sources
