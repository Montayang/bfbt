"""Deterministic SHA-256 helpers for files and metadata models."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel


SHA256_HEX_LENGTH = 64


def sha256_bytes(payload: bytes) -> str:
    """Hash an in-memory byte sequence."""

    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    """Hash file bytes without loading the whole object into memory."""

    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json_bytes(value: BaseModel | dict[str, Any] | list[Any]) -> bytes:
    """Serialize JSON data deterministically for content addressing."""

    payload: Any
    if isinstance(value, BaseModel):
        payload = value.model_dump(mode="json", exclude_none=False)
    else:
        payload = value
    return json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def content_sha256(value: BaseModel | dict[str, Any] | list[Any]) -> str:
    """Hash the canonical JSON representation of a metadata object."""

    return sha256_bytes(canonical_json_bytes(value))
