"""OpenAI Files API storage, and ``file_id`` expansion for chat inputs.

muse.ai has no hosted-file concept: chat attachments are uploaded into the
account VM as ``file_ref`` items. The gateway keeps the bytes locally so the
standard OpenAI workflow (``client.files.create`` → reference ``file_id`` in a
message) works unchanged; the reference is expanded into an inline data URL
right before the prompt is built.
"""

from __future__ import annotations

import base64
import copy
import json
import logging
import mimetypes
import re
import time
import uuid
from pathlib import Path
from typing import Any

from ..errors import InvalidRequest, NotFound
from .ownership import visible

log = logging.getLogger(__name__)

_FILE_ID = re.compile(r"^file-[A-Za-z0-9_-]{1,80}$")
_SAFE_STORED = re.compile(r"^[A-Za-z0-9_.-]{1,120}$")
_MAX_INDEX_ENTRIES = 10000


class FileStore:
    def __init__(self, root: Path, index_path: Path, *,
                 max_bytes: int = 100 * 1024 * 1024,
                 ttl_seconds: float = 30 * 86400) -> None:
        self.root = root
        self.index_path = index_path
        self.max_bytes = max(0, max_bytes)
        self.ttl = max(0.0, ttl_seconds)
        self._items: dict[str, dict[str, Any]] = {}
        self.root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------ persistence
    def load(self) -> None:
        if not self.index_path.is_file():
            return
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8") or "{}")
        except (ValueError, OSError):
            log.warning("file index corrupted, starting empty")
            return
        items = data.get("files") if isinstance(data, dict) else None
        if not isinstance(items, dict):
            return
        now = time.time()
        self._items = {}
        for key, value in items.items():
            if not isinstance(value, dict):
                continue
            if self._fresh(value, now):
                self._items[key] = value
            else:
                self._unlink(value)

    def _fresh(self, entry: dict[str, Any], now: float) -> bool:
        expires_at = entry.get("expires_at")
        if expires_at is not None and now >= float(expires_at):
            return False
        if self.ttl <= 0:
            return True
        return (now - float(entry.get("created", 0))) < self.ttl

    def _persist(self) -> None:
        now = time.time()
        # Removing expired entries from the index must also remove their bytes;
        # otherwise every TTL rotation leaves orphan files in data/files.
        for file_id, entry in list(self._items.items()):
            if not self._fresh(entry, now):
                self._items.pop(file_id, None)
                self._unlink(entry)
        kept = self._items
        ordered = sorted(kept.items(), key=lambda kv: kv[1].get("created", 0),
                         reverse=True)
        for _, entry in ordered[_MAX_INDEX_ENTRIES:]:
            self._unlink(entry)
        self._items = dict(ordered[:_MAX_INDEX_ENTRIES])
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.index_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"files": self._items}, ensure_ascii=False),
                       encoding="utf-8")
        tmp.replace(self.index_path)

    def _unlink(self, entry: dict[str, Any]) -> None:
        stored = str(entry.get("stored") or "")
        if _SAFE_STORED.match(stored):
            path = self.root / stored
            if path.is_file():
                path.unlink()

    # ------------------------------------------------------------ public API
    def create(self, *, filename: str, purpose: str, data: bytes,
               mime: str = "", expires_at: float | None = None,
               owner: str | None = None) -> dict[str, Any]:
        if self.max_bytes and len(data) > self.max_bytes:
            raise InvalidRequest(
                f"file exceeds the {self.max_bytes} byte upload limit",
                param="file")
        file_id = "file-" + uuid.uuid4().hex[:24]
        stored = uuid.uuid4().hex + Path(filename or "").suffix[:16]
        if not _SAFE_STORED.match(stored):
            stored = uuid.uuid4().hex
        (self.root / stored).write_bytes(data)
        guessed = mime or mimetypes.guess_type(filename or "")[0]
        guessed = (guessed or "application/octet-stream").split(";")[0].strip()
        entry = {
            "id": file_id,
            "object": "file",
            "bytes": len(data),
            "created_at": int(time.time()),
            "created": time.time(),
            "filename": filename or "upload.bin",
            "purpose": purpose or "assistants",
            "mime": guessed or "application/octet-stream",
            "stored": stored,
            "status": "processed",
            "expires_at": int(expires_at) if expires_at else None,
            "status_details": None,
            "owner": owner,
        }
        self._items[file_id] = entry
        self._persist()
        return self._view(entry)

    @staticmethod
    def _view(entry: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": entry["id"],
            "object": "file",
            "bytes": entry["bytes"],
            "created_at": entry["created_at"],
            "filename": entry["filename"],
            "purpose": entry["purpose"],
            "status": entry.get("status", "processed"),
            "expires_at": entry.get("expires_at"),
            "status_details": entry.get("status_details"),
        }

    def get(self, file_id: str, owner: str | None = None) -> dict[str, Any] | None:
        entry = self._items.get(file_id)
        if entry is None:
            return None
        if not self._fresh(entry, time.time()):
            self._items.pop(file_id, None)
            self._unlink(entry)
            return None
        if not visible(entry.get("owner"), owner):
            return None
        return entry

    def retrieve(self, file_id: str, owner: str | None = None) -> dict[str, Any]:
        entry = self.get(file_id, owner)
        if entry is None:
            raise NotFound(f"No file found with id '{file_id}'.", param="file_id")
        return self._view(entry)

    def read(self, file_id: str,
             owner: str | None = None) -> tuple[bytes, dict[str, Any]]:
        entry = self.get(file_id, owner)
        if entry is None:
            raise NotFound(f"No file found with id '{file_id}'.", param="file_id")
        stored = str(entry.get("stored") or "")
        path = self.root / stored
        if not _SAFE_STORED.match(stored) or not path.is_file():
            raise NotFound(f"No file found with id '{file_id}'.", param="file_id")
        return path.read_bytes(), entry

    def delete(self, file_id: str, owner: str | None = None) -> dict[str, Any]:
        entry = self.get(file_id, owner)
        if entry is None:
            raise NotFound(f"No file found with id '{file_id}'.", param="file_id")
        self._items.pop(file_id, None)
        stored = str(entry.get("stored") or "")
        if _SAFE_STORED.match(stored):
            path = self.root / stored
            if path.is_file():
                path.unlink()
        self._persist()
        return {"id": file_id, "object": "file", "deleted": True}

    def list(self, *, purpose: str | None = None, limit: int = 10000,
             order: str = "desc", after: str | None = None,
             owner: str | None = None) -> list[dict[str, Any]]:
        now = time.time()
        entries = [e for e in self._items.values()
                   if self._fresh(e, now) and visible(e.get("owner"), owner)]
        if purpose:
            entries = [e for e in entries if e.get("purpose") == purpose]
        reverse = order != "asc"

        def sort_key(entry: dict[str, Any]) -> tuple[float, str]:
            # The id breaks ties: several files can be created in the same
            # clock tick, and a timestamp-only cursor would skip that group.
            return (float(entry.get("created", 0)),
                    str(entry.get("id") or ""))

        entries.sort(key=sort_key, reverse=reverse)
        if after:
            cursor = self._items.get(after)
            if cursor is not None and visible(cursor.get("owner"), owner):
                cursor_key = sort_key(cursor)
                if reverse:
                    entries = [e for e in entries if sort_key(e) < cursor_key]
                else:
                    entries = [e for e in entries if sort_key(e) > cursor_key]
        if limit > 0:
            entries = entries[:limit]
        return [self._view(e) for e in entries]

    def prune(self, max_age_seconds: float) -> int:
        now = time.time()
        expired = [
            k for k, v in self._items.items()
            if not self._fresh(v, now)
            or (max_age_seconds > 0
                and now - float(v.get("created", 0)) > max_age_seconds)
        ]
        for file_id in expired:
            entry = self._items.pop(file_id, None) or {}
            self._unlink(entry)
        if expired:
            self._persist()
        return len(expired)

    # ------------------------------------------------------------ message refs
    def expand_messages(self, messages: list[dict[str, Any]],
                        owner: str | None = None) -> list[dict[str, Any]]:
        """Replace ``file_id`` references with inline data URLs.

        Called before prompt building. Every OpenAI attachment shape the gateway
        accepts is handled: chat ``file`` parts, Responses ``input_file`` items,
        and vision ``input_image`` / ``image_url`` + ``file_id`` references.
        """
        expanded = copy.deepcopy(messages)
        for message in expanded:
            content = message.get("content")
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict):
                        self._expand_part(part, owner)
        return expanded

    def _expand_part(self, part: dict[str, Any], owner: str | None = None) -> None:
        ptype = part.get("type")
        if ptype in ("file", "input_file"):
            nested = part.get("file")
            holder = nested if isinstance(nested, dict) else part
            file_id = str(holder.get("file_id") or part.get("file_id") or "")
            if file_id:
                data, entry = self.read(file_id, owner)
                holder["file_data"] = self._data_url(data, entry)
                holder["filename"] = holder.get("filename") or entry["filename"]
                holder.pop("file_id", None)
            return
        if ptype == "input_image":
            file_id = str(part.get("file_id") or "")
            if file_id:
                data, entry = self.read(file_id, owner)
                part["image_url"] = self._data_url(data, entry)
                part.pop("file_id", None)
            return
        if ptype == "image_url":
            ref = part.get("image_url")
            file_id = ""
            if isinstance(ref, dict):
                file_id = str(ref.get("file_id") or "")
            if file_id:
                data, entry = self.read(file_id, owner)
                part["image_url"] = {"url": self._data_url(data, entry)}

    @staticmethod
    def _data_url(data: bytes, entry: dict[str, Any]) -> str:
        mime = str(entry.get("mime") or "application/octet-stream")
        return f"data:{mime};base64,{base64.b64encode(data).decode()}"
