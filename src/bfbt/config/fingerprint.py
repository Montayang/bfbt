"""Stable serialization and SHA-256 fingerprinting for resolved configs."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel


def _canonicalize(value: Any, project_root: Path) -> Any:
    if isinstance(value, BaseModel):
        return _canonicalize(value.model_dump(mode="python"), project_root)
    if isinstance(value, dict):
        return {key: _canonicalize(value[key], project_root) for key in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_canonicalize(item, project_root) for item in value]
    if isinstance(value, Path):
        relative = os.path.relpath(value.resolve(), project_root)
        return Path(relative).as_posix()
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    return value


def canonical_config(config: BaseModel, *, project_root: Path) -> dict[str, Any]:
    """Return the deterministic, JSON-compatible form used for manifests."""

    canonical = _canonicalize(config, project_root.resolve())
    if not isinstance(canonical, dict):
        raise TypeError("top-level configuration must serialize as a mapping")
    return canonical


def config_fingerprint(config: BaseModel, *, project_root: Path) -> str:
    """Return a stable SHA-256 over the fully expanded configuration."""

    payload = json.dumps(
        canonical_config(config, project_root=project_root),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
