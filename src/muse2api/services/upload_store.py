"""OpenAI Uploads API storage (resumable multipart file uploads).

Large clients split a file into <=64 MiB parts, then ``complete`` the upload to
receive a regular Files object. muse.ai only accepts inline attachment bytes,
so the gateway stores the parts locally and assembles them into
:class:`~muse2api.services.file_store.FileStore` on completion.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import uuid
from pathlib import Path
from typing import Any

from ..errors import InvalidRequest, NotFound
from .file_store import FileStore
from .ownership import visible

log = logging.getLogger(__name__)

_MAX_PARTS = 128
MAX_PART_BYTES = 64 * 1024 * 1024
_MAX_INDEX_ENTRIES = 1000
_SAFE_STORED = re.compile(r"^[A-Za-z0-9_.-]{1,120}$")


class UploadStore:
    def __init__(self, root: Path, index_path: Path, files: FileStore, *,
                 ttl_seconds: float = 3600) -> None:
        self.root = root
        self.index_path = index_path
        self.files = files
        self.ttl = max(60.0, ttl_seconds)
        self._uploads: dict[str, dict[str, Any]] = {}
        self.root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------ persistence
    def load(self) -> None:
        if not self.index_path.is_file():
            return
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8") or "{}")
        except (ValueError, OSError):
            log.warning("upload index corrupted, starting empty")
            return
        items = data.get("uploads") if isinstance(data, dict) else None
        if not isinstance(items, dict):
            return
        now = time.time()
        self._uploads = {}
        for key, value in items.items():
            if not isinstance(value, dict):
                continue
            if self._fresh(value, now):
                self._uploads[key] = value
            else:
                self._cleanup_parts(value)

    def _fresh(self, entry: dict[str, Any], now: float) -> bool:
        expires_at = float(entry.get("expires_at") or 0)
        return now < expires_at

    def _persist(self) -> None:
        now = time.time()
        for upload_id, entry in list(self._uploads.items()):
            if not self._fresh(entry, now):
                self._uploads.pop(upload_id, None)
                self._cleanup_parts(entry)
        ordered = sorted(self._uploads.items(),
                         key=lambda kv: kv[1].get("created_at", 0), reverse=True)
        for _, entry in ordered[_MAX_INDEX_ENTRIES:]:
            self._cleanup_parts(entry)
        self._uploads = dict(ordered[:_MAX_INDEX_ENTRIES])
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.index_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"uploads": self._uploads}, ensure_ascii=False),
                       encoding="utf-8")
        tmp.replace(self.index_path)

    def _part_path(self, part: dict[str, Any]) -> Path | None:
        stored = str(part.get("stored") or "")
        if not _SAFE_STORED.match(stored):
            return None
        return self.root / stored

    def _cleanup_parts(self, entry: dict[str, Any]) -> None:
        for part in (entry.get("parts") or {}).values():
            path = self._part_path(part)
            if path is not None and path.is_file():
                path.unlink()

    def _get(self, upload_id: str, owner: str | None) -> dict[str, Any] | None:
        entry = self._uploads.get(upload_id)
        if entry is None:
            return None
        if not self._fresh(entry, time.time()):
            self._uploads.pop(upload_id, None)
            self._cleanup_parts(entry)
            return None
        if not visible(entry.get("owner"), owner):
            return None
        return entry

    # ------------------------------------------------------------------- views
    def _view(self, entry: dict[str, Any]) -> dict[str, Any]:
        view = {
            "id": entry["id"],
            "object": "upload",
            "bytes": entry["bytes"],
            "created_at": entry["created_at"],
            "expires_at": entry["expires_at"],
            "filename": entry["filename"],
            "purpose": entry["purpose"],
            "status": entry["status"],
        }
        file_id = entry.get("file_id")
        if file_id:
            try:
                view["file"] = self.files.retrieve(file_id, entry.get("owner"))
            except NotFound:
                # The assembled file can outlive the upload record only if it
                # was deleted; keep the upload shape without the stale object.
                view["file"] = None
        return view

    @staticmethod
    def _part_view(part: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": part["id"],
            "object": "upload.part",
            "created_at": part["created_at"],
            "upload_id": part["upload_id"],
        }

    # ------------------------------------------------------------ public API
    def create(self, *, filename: str, purpose: str, mime_type: str,
               total_bytes: int, owner: str | None = None,
               file_expires_at: float | None = None) -> dict[str, Any]:
        max_bytes = self.files.max_bytes
        if total_bytes <= 0:
            raise InvalidRequest("bytes must be greater than 0", param="bytes")
        if max_bytes and total_bytes > max_bytes:
            raise InvalidRequest(
                f"upload exceeds the {max_bytes} byte file limit", param="bytes")
        now = time.time()
        upload_id = "upload_" + uuid.uuid4().hex[:24]
        entry = {
            "id": upload_id,
            "object": "upload",
            "bytes": total_bytes,
            "created_at": int(now),
            "expires_at": int(now + self.ttl),
            "file_expires_at": int(file_expires_at) if file_expires_at else None,
            "filename": filename,
            "purpose": purpose,
            "mime_type": mime_type,
            "status": "pending",
            "owner": owner,
            "parts": {},
        }
        self._uploads[upload_id] = entry
        self._persist()
        return self._view(entry)

    def add_part(self, upload_id: str, data: bytes,
                 owner: str | None = None) -> dict[str, Any] | None:
        entry = self._get(upload_id, owner)
        if entry is None:
            return None
        if entry["status"] != "pending":
            raise InvalidRequest(
                f"upload '{upload_id}' is {entry['status']} and cannot accept parts",
                param="upload_id", code="upload_not_pending")
        if not data:
            raise InvalidRequest("part data must not be empty", param="data")
        if len(data) > MAX_PART_BYTES:
            raise InvalidRequest(
                f"each part must be at most {MAX_PART_BYTES} bytes", param="data")
        parts = entry["parts"]
        if len(parts) >= _MAX_PARTS:
            raise InvalidRequest(
                f"upload may contain at most {_MAX_PARTS} parts", param="data")
        uploaded = sum(int(part.get("bytes", 0)) for part in parts.values())
        if uploaded + len(data) > int(entry["bytes"]):
            raise InvalidRequest(
                f"parts exceed the declared upload size of {entry['bytes']} bytes",
                param="data", code="upload_size_exceeded")
        part_id = "part_" + uuid.uuid4().hex[:24]
        stored = uuid.uuid4().hex
        (self.root / stored).write_bytes(data)
        part = {
            "id": part_id,
            "object": "upload.part",
            "created_at": int(time.time()),
            "upload_id": upload_id,
            "stored": stored,
            "bytes": len(data),
        }
        parts[part_id] = part
        self._persist()
        return self._part_view(part)

    def complete(self, upload_id: str, part_ids: list[str], *,
                 md5: str | None = None,
                 owner: str | None = None) -> dict[str, Any] | None:
        entry = self._get(upload_id, owner)
        if entry is None:
            return None
        if entry["status"] != "pending":
            raise InvalidRequest(
                f"upload '{upload_id}' is {entry['status']} and cannot be completed",
                param="upload_id", code="upload_not_pending")
        if not part_ids:
            raise InvalidRequest("part_ids must contain at least one id",
                                 param="part_ids")
        if len(set(part_ids)) != len(part_ids):
            raise InvalidRequest("part_ids contains duplicate ids", param="part_ids")
        parts = entry["parts"]
        unknown = [part_id for part_id in part_ids if part_id not in parts]
        if unknown:
            raise InvalidRequest(
                f"unknown part id '{unknown[0]}'", param="part_ids",
                code="invalid_part")
        ordered = [parts[part_id] for part_id in part_ids]
        total = sum(int(part.get("bytes", 0)) for part in ordered)
        if total != int(entry["bytes"]):
            raise InvalidRequest(
                f"uploaded {total} bytes but the upload declared {entry['bytes']}",
                param="part_ids", code="upload_size_mismatch")
        chunks: list[bytes] = []
        for part in ordered:
            path = self._part_path(part)
            if path is None or not path.is_file():
                raise InvalidRequest(
                    f"bytes for part '{part['id']}' are no longer available",
                    param="part_ids", code="invalid_part")
            chunks.append(path.read_bytes())
        data = b"".join(chunks)
        if md5 is not None:
            expected = str(md5).strip().lower()
            if expected != hashlib.md5(data).hexdigest():
                raise InvalidRequest("md5 checksum does not match the uploaded bytes",
                                     param="md5", code="md5_mismatch")
        file_view = self.files.create(
            filename=entry["filename"], purpose=entry["purpose"], data=data,
            mime=entry["mime_type"],
            expires_at=entry.get("file_expires_at"),
            owner=entry.get("owner"))
        entry["status"] = "completed"
        entry["file_id"] = file_view["id"]
        entry["completed_at"] = int(time.time())
        self._cleanup_parts(entry)
        entry["parts"] = {}
        self._persist()
        return self._view(entry)

    def cancel(self, upload_id: str,
               owner: str | None = None) -> dict[str, Any] | None:
        entry = self._get(upload_id, owner)
        if entry is None:
            return None
        if entry["status"] == "completed":
            raise InvalidRequest(
                f"upload '{upload_id}' is already completed", param="upload_id",
                code="upload_completed")
        if entry["status"] == "cancelled":
            return self._view(entry)
        entry["status"] = "cancelled"
        self._cleanup_parts(entry)
        entry["parts"] = {}
        self._persist()
        return self._view(entry)

    def prune(self) -> int:
        now = time.time()
        expired = [upload_id for upload_id, entry in self._uploads.items()
                   if not self._fresh(entry, now)]
        for upload_id in expired:
            entry = self._uploads.pop(upload_id, None) or {}
            self._cleanup_parts(entry)
        if expired:
            self._persist()
        return len(expired)
