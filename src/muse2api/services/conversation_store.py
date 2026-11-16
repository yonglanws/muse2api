"""OpenAI Conversations API storage and item conversion.

A conversation is a gateway-local list of Responses input items. Responses can
prepend it to ``input`` and append the new turn, so clients that use
``conversation=...`` instead of ``previous_response_id`` get the same stateful
behavior. muse.ai itself only sees the flattened messages.
"""

from __future__ import annotations

import copy
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any

from ..errors import InvalidRequest
from .ownership import visible

log = logging.getLogger(__name__)

_MAX_ENTRIES = 2000
_DEFAULT_LIMIT = 20
_MAX_LIMIT = 100


def _new_item_id(item_type: str | None) -> str:
    prefix = {
        "message": "msg",
        "function_call": "fc",
        "function_call_output": "fco",
    }.get(item_type or "", "item")
    return f"{prefix}_{uuid.uuid4().hex[:24]}"


def _text_type(role: str) -> str:
    return "output_text" if role == "assistant" else "input_text"


def _normalize_content(content: Any, role: str) -> list[dict[str, Any]]:
    if content is None:
        return []
    if isinstance(content, str):
        part: dict[str, Any] = {"type": _text_type(role), "text": content}
        if part["type"] == "output_text":
            part.update(annotations=[], logprobs=[])
        return [part]
    if not isinstance(content, list):
        content = [content]
    out: list[dict[str, Any]] = []
    for raw in content:
        if not isinstance(raw, dict):
            continue
        part = copy.deepcopy(raw)
        ptype = part.get("type")
        if ptype is None and "text" in part:
            ptype = _text_type(role)
            part["type"] = ptype
        if ptype in ("text", "input_text", "output_text"):
            part.setdefault("type", _text_type(role))
            part["text"] = str(part.get("text") or "")
            if part["type"] == "output_text":
                part.setdefault("annotations", [])
                part.setdefault("logprobs", [])
        out.append(part)
    return out


def normalize_item(raw: Any) -> dict[str, Any]:
    """Give a conversation item the id/type/status fields the API promises."""
    if not isinstance(raw, dict):
        raise InvalidRequest("conversation items must be objects", param="items")
    item = copy.deepcopy(raw)
    item_type = item.get("type")
    if item_type is None:
        role = str(item.get("role") or "user")
        item = {"type": "message", "role": role,
                "content": _normalize_content(item.get("content"), role)}
        item_type = "message"
    elif item_type == "message":
        role = str(item.get("role") or "user")
        item["role"] = role
        item["content"] = _normalize_content(item.get("content"), role)
    elif item_type == "item_reference" and not item.get("id"):
        raise InvalidRequest("item_reference requires an 'id'", param="items")
    if not item.get("id"):
        item["id"] = _new_item_id(item_type)
    if item_type in ("message", "function_call", "function_call_output"):
        item.setdefault("status", "completed")
    return item


def _tool_output(content: Any) -> Any:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[dict[str, Any]] = []
        for raw in content:
            if not isinstance(raw, dict):
                continue
            part = copy.deepcopy(raw)
            if part.get("type") == "text":
                part["type"] = "input_text"
            parts.append(part)
        return parts
    return str(content)


def messages_to_items(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert the chat messages used internally back into Responses items."""
    items: list[dict[str, Any]] = []
    for message in messages:
        role = str(message.get("role") or "user")
        content = message.get("content")
        if role == "tool":
            items.append(normalize_item({
                "type": "function_call_output",
                "call_id": message.get("tool_call_id") or "",
                "output": _tool_output(content)}))
            continue
        calls = message.get("tool_calls") or []
        legacy = message.get("function_call")
        if legacy:
            items.append(normalize_item({
                "type": "function_call",
                "call_id": message.get("tool_call_id") or "",
                "name": legacy.get("name") or "",
                "arguments": legacy.get("arguments") or ""}))
            if content:
                items.append(normalize_item(
                    {"type": "message", "role": role, "content": content}))
            continue
        if calls:
            if content:
                items.append(normalize_item(
                    {"type": "message", "role": role, "content": content}))
            for call in calls:
                function = call.get("function") or {}
                items.append(normalize_item({
                    "type": "function_call",
                    "call_id": call.get("id") or "",
                    "name": function.get("name") or "",
                    "arguments": function.get("arguments") or ""}))
            continue
        items.append(normalize_item(
            {"type": "message", "role": role, "content": content}))
    return items


def _chat_content(parts: Any, role: str) -> Any:
    if isinstance(parts, str):
        return parts
    if not isinstance(parts, list):
        return str(parts or "")
    texts: list[str] = []
    attachments: list[dict[str, Any]] = []
    for raw in parts:
        if not isinstance(raw, dict):
            continue
        ptype = raw.get("type")
        if ptype in ("text", "input_text", "output_text", "summary_text"):
            texts.append(str(raw.get("text") or ""))
        elif ptype == "refusal":
            texts.append(str(raw.get("refusal") or ""))
        elif ptype == "input_image":
            if raw.get("file_id"):
                attachments.append({"type": "input_image",
                                    "file_id": raw["file_id"]})
            else:
                url = raw.get("image_url")
                if isinstance(url, str):
                    url = {"url": url}
                attachments.append({"type": "image_url", "image_url": url or {}})
        elif ptype in ("input_file", "file"):
            nested = raw.get("file") if isinstance(raw.get("file"), dict) else None
            attachments.append(
                {"type": "file", "file": nested or {
                    key: value for key, value in raw.items()
                    if key not in ("type", "id", "status")}})
    if attachments:
        prefix = ([{"type": "text", "text": "\n".join(texts)}]
                  if texts else [])
        return prefix + attachments
    return "\n".join(texts)


def items_to_messages(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten conversation items into the chat messages the planner needs."""
    by_id = {
        str(item["id"]): item for item in items
        if isinstance(item, dict) and item.get("id")
        and item.get("type") != "item_reference"
    }
    messages: list[dict[str, Any]] = []

    def add(item: dict[str, Any], seen: set[str]) -> None:
        item_type = item.get("type")
        if item_type == "item_reference":
            reference = str(item.get("id") or "")
            target = by_id.get(reference)
            if target is not None and reference not in seen:
                add(target, seen | {reference})
            return
        if item_type == "message":
            role = str(item.get("role") or "user")
            content = _chat_content(item.get("content"), role)
            if content:
                messages.append({"role": role, "content": content})
        elif item_type == "function_call":
            messages.append({
                "role": "assistant", "content": None,
                "tool_calls": [{
                    "id": item.get("call_id") or item.get("id"),
                    "type": "function",
                    "function": {"name": item.get("name") or "",
                                 "arguments": item.get("arguments") or ""}}]})
        elif item_type == "function_call_output":
            output = item.get("output")
            if isinstance(output, list):
                content = _chat_content(output, "tool")
            elif isinstance(output, str):
                content = output
            else:
                content = json.dumps(output, ensure_ascii=False)
            messages.append({"role": "tool",
                             "tool_call_id": item.get("call_id") or "",
                             "content": content})

    for item in items:
        if isinstance(item, dict):
            add(item, set())
    return messages


class ConversationStore:
    def __init__(self, path: Path, max_entries: int = _MAX_ENTRIES) -> None:
        self.path = path
        self.max_entries = max(1, max_entries)
        self._items: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------ persistence
    def load(self) -> None:
        if not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8") or "{}")
        except (ValueError, OSError):
            log.warning("conversation file corrupted, starting empty")
            return
        items = data.get("conversations") if isinstance(data, dict) else None
        if isinstance(items, dict):
            self._items = {key: value for key, value in items.items()
                           if isinstance(value, dict)}

    def _persist(self) -> None:
        ordered = sorted(self._items.items(),
                         key=lambda kv: kv[1].get("created_at", 0), reverse=True)
        self._items = dict(ordered[:self.max_entries])
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"conversations": self._items},
                                  ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    # ------------------------------------------------------------------ views
    @staticmethod
    def _view(entry: dict[str, Any]) -> dict[str, Any]:
        return {"id": entry["id"], "object": "conversation",
                "created_at": entry["created_at"],
                "metadata": copy.deepcopy(entry.get("metadata") or {})}

    @staticmethod
    def _item_view(item: dict[str, Any]) -> dict[str, Any]:
        return copy.deepcopy(item)

    def _entry(self, conversation_id: str,
               owner: str | None) -> dict[str, Any] | None:
        entry = self._items.get(conversation_id)
        if entry is None or not visible(entry.get("owner"), owner):
            return None
        return entry

    # ------------------------------------------------------------ public API
    def create(self, *, metadata: dict[str, str] | None = None,
               items: list[dict[str, Any]] | None = None,
               owner: str | None = None) -> dict[str, Any]:
        conversation_id = "conv_" + uuid.uuid4().hex[:24]
        entry = {
            "id": conversation_id,
            "created_at": int(time.time()),
            "metadata": copy.deepcopy(metadata) if metadata else {},
            "owner": owner,
            "items": [normalize_item(item) for item in (items or [])],
        }
        self._items[conversation_id] = entry
        self._persist()
        return self._view(entry)

    def get(self, conversation_id: str,
            owner: str | None = None) -> dict[str, Any] | None:
        entry = self._entry(conversation_id, owner)
        return self._view(entry) if entry else None

    def entry(self, conversation_id: str,
              owner: str | None = None) -> dict[str, Any] | None:
        return self._entry(conversation_id, owner)

    def update_metadata(self, conversation_id: str, metadata: dict[str, str] | None,
                        owner: str | None = None) -> dict[str, Any] | None:
        entry = self._entry(conversation_id, owner)
        if entry is None:
            return None
        entry["metadata"] = copy.deepcopy(metadata) if metadata else {}
        self._persist()
        return self._view(entry)

    def delete(self, conversation_id: str, owner: str | None = None) -> bool:
        if self._entry(conversation_id, owner) is None:
            return False
        self._items.pop(conversation_id, None)
        self._persist()
        return True

    def add_items(self, conversation_id: str, raw_items: list[dict[str, Any]],
                  owner: str | None = None) -> list[dict[str, Any]] | None:
        entry = self._entry(conversation_id, owner)
        if entry is None:
            return None
        items = [normalize_item(item) for item in raw_items]
        entry["items"].extend(items)
        self._persist()
        return [self._item_view(item) for item in items]

    def list_items(self, conversation_id: str, *, after: str | None = None,
                   limit: int = _DEFAULT_LIMIT, order: str = "desc",
                   owner: str | None = None) -> dict[str, Any] | None:
        entry = self._entry(conversation_id, owner)
        if entry is None:
            return None
        limit = max(1, min(int(limit), _MAX_LIMIT))
        ids = [item["id"] for item in entry["items"]]
        if order == "asc":
            ordered = ids
        elif order == "desc":
            ordered = list(reversed(ids))
        else:
            raise ValueError("order must be 'asc' or 'desc'")
        start = 0
        if after:
            try:
                start = ordered.index(after) + 1
            except ValueError:
                raise ValueError(f"invalid 'after' cursor: {after}") from None
        window = ordered[start:start + limit]
        by_id = {item["id"]: item for item in entry["items"]}
        data = [self._item_view(by_id[item_id]) for item_id in window]
        return {"object": "list", "data": data,
                "first_id": window[0] if window else None,
                "last_id": window[-1] if window else None,
                "has_more": start + len(window) < len(ordered)}

    def get_item(self, conversation_id: str, item_id: str,
                 owner: str | None = None) -> dict[str, Any] | None:
        entry = self._entry(conversation_id, owner)
        if entry is None:
            return None
        for item in entry["items"]:
            if item.get("id") == item_id:
                return self._item_view(item)
        return None

    def delete_item(self, conversation_id: str, item_id: str,
                    owner: str | None = None) -> dict[str, Any] | None:
        entry = self._entry(conversation_id, owner)
        if entry is None:
            return None
        for index, item in enumerate(entry["items"]):
            if item.get("id") == item_id:
                entry["items"].pop(index)
                self._persist()
                return self._view(entry)
        return None
