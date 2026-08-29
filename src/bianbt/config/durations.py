"""Strict duration and bar-interval helpers."""

from __future__ import annotations

import re

_DURATION_PATTERN = re.compile(r"^(?P<amount>[1-9][0-9]*)(?P<unit>s|m|h|d|w)$")
_UNIT_SECONDS = {
    "s": 1,
    "m": 60,
    "h": 60 * 60,
    "d": 24 * 60 * 60,
    "w": 7 * 24 * 60 * 60,
}


def duration_seconds(value: str) -> int:
    """Return seconds for an unambiguous duration such as ``15m`` or ``4h``."""

    match = _DURATION_PATTERN.fullmatch(value)
    if match is None:
        raise ValueError(
            "must use a positive integer followed by s, m, h, d, or w"
        )
    return int(match.group("amount")) * _UNIT_SECONDS[match.group("unit")]


def is_integer_multiple(value: str, base: str) -> bool:
    """Return whether ``value`` is an integer multiple of ``base``."""

    value_seconds = duration_seconds(value)
    base_seconds = duration_seconds(base)
    return value_seconds >= base_seconds and value_seconds % base_seconds == 0
