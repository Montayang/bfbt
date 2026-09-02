"""Small standard-library compatibility helpers for supported Python versions."""

from __future__ import annotations

try:
    from enum import StrEnum
except ImportError:  # pragma: no cover - exercised by the Python 3.10 CI job
    from enum import Enum

    class StrEnum(str, Enum):
        """Python 3.10-compatible subset of :class:`enum.StrEnum`."""

        def __str__(self) -> str:
            return str.__str__(self)


__all__ = ["StrEnum"]
