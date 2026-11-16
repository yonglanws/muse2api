"""Validation for parameters shared by Chat Completions and Responses."""

from __future__ import annotations

from typing import Any

from ..errors import InvalidRequest

_MAX_PAIRS = 16
_MAX_KEY = 64
_MAX_VALUE = 512


def validate_metadata(metadata: dict[str, Any] | None) -> dict[str, str] | None:
    """Enforce OpenAI's metadata bounds (16 string pairs, 64/512 chars)."""
    if metadata is None:
        return None
    if len(metadata) > _MAX_PAIRS:
        raise InvalidRequest(
            f"metadata may contain at most {_MAX_PAIRS} key-value pairs",
            param="metadata")
    for key, value in metadata.items():
        if not isinstance(key, str) or len(key) > _MAX_KEY:
            raise InvalidRequest(
                f"metadata keys must be strings of at most {_MAX_KEY} characters",
                param="metadata")
        if not isinstance(value, str) or len(value) > _MAX_VALUE:
            raise InvalidRequest(
                "metadata values must be strings of at most "
                f"{_MAX_VALUE} characters", param="metadata")
    return metadata
