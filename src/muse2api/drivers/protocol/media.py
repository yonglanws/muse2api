"""Helpers for Muse's generated-media envelopes and sandbox resources."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
WEBP_RIFF = b"RIFF"
WEBP_WEBP = b"WEBP"


def image_magic_mime(data: bytes) -> str | None:
    if not data:
        return None
    if data.startswith(PNG_MAGIC):
        return "image/png"
    if len(data) >= 12 and data.startswith(WEBP_RIFF) and data[8:12] == WEBP_WEBP:
        return "image/webp"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return "image/gif"
    if len(data) >= 12 and data[4:8] == b"ftyp":
        return "video/mp4"
    return None


def _normalize_media_path(path: str) -> tuple[str, str] | None:
    if not isinstance(path, str) or not path:
        return None
    if path.startswith("sandbox://"):
        remote = path[len("sandbox://"):]
        uri = path
    elif path.startswith("workspace/"):
        remote = path
        uri = "sandbox://" + path
    else:
        return None
    if remote.startswith("workspace/user/"):
        return None
    return uri, remote


def _media_candidates(value: Any, output: list[dict[str, Any]]) -> None:
    """Collect generated sandbox resources from presentation/resource envelopes."""
    if isinstance(value, dict):
        meta = value.get("metadata")
        if isinstance(meta, str) and meta.strip().startswith("{"):
            try:
                parsed = json.loads(meta)
            except (TypeError, ValueError, json.JSONDecodeError):
                parsed = None
            if isinstance(parsed, dict):
                value = {**value, "metadata": parsed}
                _media_candidates(parsed, output)

        path = (value.get("path") or value.get("uri")
                or value.get("image_path") or value.get("video_path"))
        normalized = _normalize_media_path(path) if isinstance(path, str) else None
        if normalized is None and isinstance(value.get("metadata"), dict):
            meta = value["metadata"]
            path = meta.get("path") or meta.get("uri")
            normalized = _normalize_media_path(path) if isinstance(path, str) else None
            if normalized is not None:
                value = {
                    **value,
                    "path": normalized[0],
                    "mime": value.get("mime") or value.get("mime_type") or meta.get("mime_type"),
                    "mime_type": value.get("mime_type") or meta.get("mime_type"),
                    "width": value.get("width") or meta.get("width"),
                    "height": value.get("height") or meta.get("height"),
                    "byte_len": value.get("byte_len") or meta.get("byte_len")
                    or meta.get("size_bytes"),
                }
        if normalized is not None:
            uri, _remote = normalized
            resource = dict(value)
            resource["path"] = uri
            if not any(item["resource"].get("path") == uri for item in output):
                output.append({"resource": resource})
        for key, child in value.items():
            if key not in ("request", "data_base64"):
                _media_candidates(child, output)
    elif isinstance(value, list):
        for child in value:
            _media_candidates(child, output)


def resource_kind(resource: dict[str, Any]) -> str:
    mime = str(resource.get("mime") or resource.get("mime_type") or "").lower()
    if mime.startswith("video/"):
        return "video"
    if mime.startswith("image/"):
        return "image"
    path = str(resource.get("path") or resource.get("uri")
               or resource.get("image_path") or resource.get("video_path") or "")
    suffix = Path(path).suffix.lower()
    return "video" if suffix in (".mp4", ".webm", ".mov", ".m4v") else "image"


def resource_mime(resource: dict[str, Any], data: bytes) -> str:
    mime = str(resource.get("mime") or resource.get("mime_type") or "")
    if mime:
        return mime
    return image_magic_mime(data) or (
        "video/mp4" if resource_kind(resource) == "video" else "image/png")
