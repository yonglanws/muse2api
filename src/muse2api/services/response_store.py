"""Server-side state for the Responses API (``previous_response_id`` chains).

OpenAI's Responses API is stateful by default: every stored response can be
continued with ``previous_response_id``. We persist the *chat messages* of each
response to a JSON file so chains survive a gateway restart. Entries expire
after ``MUSE2API_RESPONSE_TTL`` seconds and are capped to keep the file small.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from .ownership import visible

log = logging.getLogger(__name__)

_MAX_ENTRIES = 1000


class ResponseStore:
    def __init__(self, path: Path, ttl_seconds: int = 7 * 86400) -> None:
        self.path = path
        self.ttl = max(0, ttl_seconds)
        self._items: dict[str, dict[str, Any]] = {}
        # In-flight responses, for POST /v1/responses/{id}/cancel. Never
        # persisted: a restart cannot cancel a turn that no longer exists.
        self._live: dict[str, dict[str, Any]] = {}
        # Ids deleted while their turn was still running; save_chain() must not
        # recreate them when the turn finishes.
        self._deleted: set[str] = set()

    def load(self) -> None:
        if not self.path.is_file():
            return
        try:
            items = json.loads(self.path.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError:
            log.warning("responses file corrupted, starting empty")
            return
        if not isinstance(items, dict):
            return
        now = time.time()
        self._items = {k: v for k, v in items.items() if self._fresh(v, now)}

    def _fresh(self, entry: dict[str, Any], now: float) -> bool:
        if self.ttl <= 0:
            return True
        return (now - float(entry.get("created", 0))) < self.ttl

    def _persist(self) -> None:
        now = time.time()
        # TTL first, then newest-first cap; keeps restarts cheap and the file bounded.
        kept = {k: v for k, v in self._items.items() if self._fresh(v, now)}
        ordered = sorted(kept.items(), key=lambda kv: kv[1].get("created", 0), reverse=True)
        ordered = ordered[:_MAX_ENTRIES]
        self._items = dict(ordered)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._items, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    def get(self, response_id: str,
            owner: str | None = None) -> dict[str, Any] | None:
        entry = self._items.get(response_id)
        if entry is None:
            return None
        if not self._fresh(entry, time.time()):
            self._items.pop(response_id, None)
            return None
        if not visible(entry.get("owner"), owner):
            return None
        return entry

    def save(self, response_id: str, *, model: str, messages: list[dict],
             hint: str | None, response: dict[str, Any] | None = None,
             input_messages: list[dict] | None = None,
             owner: str | None = None) -> None:
        if response_id in self._deleted:
            return
        self._items[response_id] = {
            "id": response_id,
            "created": time.time(),
            "model": model,
            "messages": messages,
            "hint": hint,
            # The serialized response object so GET /v1/responses/{id} returns
            # exactly what the create call returned (0.11.0).
            "response": response,
            # The request's unconverted chat messages, for input_items.
            "input_messages": input_messages if input_messages is not None else messages,
            "owner": owner,
        }
        self._persist()

    def delete(self, response_id: str, owner: str | None = None) -> bool:
        if self.get(response_id, owner) is None:
            return False
        self._items.pop(response_id, None)
        self._deleted.add(response_id)
        self._persist()
        return True

    # ------------------------------------------------------------ live turns
    def start_live(self, response_id: str, cancel, snapshot: dict[str, Any],
                   owner: str | None = None) -> None:
        self._live[response_id] = {"cancel": cancel, "snapshot": snapshot,
                                   "owner": owner}

    def finish_live(self, response_id: str) -> None:
        self._live.pop(response_id, None)

    def cancel_live(self, response_id: str,
                    owner: str | None = None) -> dict[str, Any] | None:
        live = self._live.get(response_id)
        if live is None or not visible(live.get("owner"), owner):
            return None
        cancel = live.get("cancel")
        if cancel is not None:
            cancel.set()
        snapshot = dict(live.get("snapshot") or {})
        snapshot["status"] = "cancelled"
        return snapshot
