"""Stored Chat Completions (OpenAI's ``store: true`` surface).

OpenAI keeps chat completions created with ``store: true`` so they can be
listed, retrieved, re-labelled with metadata, deleted, and inspected through
``GET /v1/chat/completions/{id}/messages``. The messages endpoint returns the
*input* messages of the request (the generated output already lives in the
completion object).

Like :class:`~muse2api.services.response_store.ResponseStore` this is a small
JSON file with a TTL and a hard entry cap: the gateway is single-node and the
data is only needed for API parity, not as a durable transcript archive.
"""

from __future__ import annotations

import copy
import json
import logging
import time
from pathlib import Path
from typing import Any

from .ownership import visible

log = logging.getLogger(__name__)

_MAX_ENTRIES = 2000
_DEFAULT_LIMIT = 20
_MAX_LIMIT = 100


def _page(entry_ids: list[str], items: dict[str, Any], *,
          after: str | None, limit: int, order: str) -> dict[str, Any]:
    """Cursor pagination over ``entry_ids`` already in ``order``."""
    if order == "asc":
        ordered = entry_ids
    elif order == "desc":
        ordered = list(reversed(entry_ids))
    else:
        raise ValueError("order must be 'asc' or 'desc'")
    start = 0
    if after:
        try:
            start = ordered.index(after) + 1
        except ValueError:
            raise ValueError(f"invalid 'after' cursor: {after}") from None
    window = ordered[start:start + limit]
    return {
        "object": "list",
        "data": [items[i] for i in window],
        "first_id": window[0] if window else None,
        "last_id": window[-1] if window else None,
        "has_more": start + len(window) < len(ordered),
    }


class ChatCompletionStore:
    def __init__(self, path: Path, ttl_seconds: int = 30 * 86400,
                 max_entries: int = _MAX_ENTRIES) -> None:
        self.path = path
        self.ttl = max(0, ttl_seconds)
        self.max_entries = max(1, max_entries)
        self._items: dict[str, dict[str, Any]] = {}

    # --------------------------------------------------------------- lifecycle
    def load(self) -> None:
        if not self.path.is_file():
            return
        try:
            items = json.loads(self.path.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError:
            log.warning("stored completions file corrupted, starting empty")
            return
        if not isinstance(items, dict):
            return
        now = time.time()
        self._items = {k: v for k, v in items.items() if self._fresh(v, now)}

    def _fresh(self, entry: dict[str, Any], now: float) -> bool:
        if self.ttl <= 0:
            return True
        return (now - float(entry.get("stored_at", 0))) < self.ttl

    def _persist(self) -> None:
        now = time.time()
        kept = {k: v for k, v in self._items.items() if self._fresh(v, now)}
        ordered = sorted(kept.items(), key=lambda kv: kv[1].get("stored_at", 0),
                         reverse=True)[:self.max_entries]
        self._items = dict(ordered)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._items, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    def prune(self) -> None:
        if self._items:
            self._persist()

    # ------------------------------------------------------------------- write
    def save(self, completion: dict[str, Any], messages: list[dict[str, Any]], *,
             metadata: dict[str, Any] | None = None,
             user: str | None = None,
             owner: str | None = None) -> None:
        completion = copy.deepcopy(completion)
        completion["metadata"] = copy.deepcopy(metadata) if metadata else {}
        self._items[completion["id"]] = {
            "completion": completion,
            "messages": copy.deepcopy(messages),
            "metadata": copy.deepcopy(metadata) if metadata else {},
            "user": user,
            "owner": owner,
            "model": completion.get("model"),
            "created": completion.get("created", int(time.time())),
            "stored_at": time.time(),
        }
        self._persist()

    def update_metadata(self, completion_id: str, metadata: dict[str, Any] | None,
                        owner: str | None = None) -> dict | None:
        entry = self._get_entry(completion_id, owner)
        if entry is None:
            return None
        entry["metadata"] = copy.deepcopy(metadata) if metadata else {}
        entry["completion"]["metadata"] = copy.deepcopy(entry["metadata"])
        self._persist()
        return copy.deepcopy(entry["completion"])

    def delete(self, completion_id: str, owner: str | None = None) -> bool:
        if self._get_entry(completion_id, owner) is None:
            return False
        self._items.pop(completion_id, None)
        self._persist()
        return True

    # -------------------------------------------------------------------- read
    def _get_entry(self, completion_id: str,
                   owner: str | None = None) -> dict[str, Any] | None:
        entry = self._items.get(completion_id)
        if entry is None:
            return None
        if not self._fresh(entry, time.time()):
            self._items.pop(completion_id, None)
            return None
        if not visible(entry.get("owner"), owner):
            return None
        return entry

    def get(self, completion_id: str,
            owner: str | None = None) -> dict[str, Any] | None:
        entry = self._get_entry(completion_id, owner)
        return copy.deepcopy(entry["completion"]) if entry else None

    def list(self, *, model: str | None = None, metadata: dict[str, str] | None = None,
             after: str | None = None, limit: int = _DEFAULT_LIMIT,
             order: str = "asc", owner: str | None = None) -> dict[str, Any]:
        limit = max(1, min(int(limit), _MAX_LIMIT))
        ids = [
            cid for cid, entry in sorted(
                self._items.items(),
                key=lambda kv: (kv[1].get("created", 0), kv[1].get("stored_at", 0)),
            )
            if self._fresh(entry, time.time())
            and visible(entry.get("owner"), owner)
            and (model is None or entry.get("model") == model)
            and all(entry.get("metadata", {}).get(k) == v
                    for k, v in (metadata or {}).items())
        ]
        completions = {cid: copy.deepcopy(self._items[cid]["completion"]) for cid in ids}
        return _page(ids, completions, after=after, limit=limit, order=order)

    def messages(self, completion_id: str, *, after: str | None = None,
                 limit: int = _DEFAULT_LIMIT,
                 order: str = "asc",
                 owner: str | None = None) -> dict[str, Any] | None:
        entry = self._get_entry(completion_id, owner)
        if entry is None:
            return None
        limit = max(1, min(int(limit), _MAX_LIMIT))
        stored = entry.get("messages") or []
        ids = [m.get("id") for m in stored]
        by_id = {m.get("id"): m for m in stored}
        return _page(ids, by_id, after=after, limit=limit, order=order)
