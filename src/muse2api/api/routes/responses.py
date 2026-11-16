"""OpenAI Responses API (``/v1/responses``).

0.11.0 brings this route to the full OpenAI object surface:

- **Stateful**: ``previous_response_id`` continues a stored conversation
  (``data/responses.json``, TTL ``MUSE2API_RESPONSE_TTL``); ``store`` controls
  whether the response is storable at all. ``GET``/``DELETE /{id}``,
  ``/{id}/input_items``, ``/input_tokens`` and ``/{id}/cancel`` are implemented.
- **Function calling**: request ``tools`` / ``tool_choice`` ride the same
  planner as chat completions; both the Chat (nested ``function``) and
  Responses (top-level ``name``) tool shapes are accepted. ``function_call`` /
  ``function_call_output`` input items are accepted and answers come back as
  ``function_call`` output items (streaming emits
  ``response.function_call_arguments.*``).
- **Web search**: ``{"type": "web_search"}`` drives muse's native browser and
  returns real ``url_citation`` annotations.
- **Complete event stream**: ``response.created`` / ``response.in_progress`` /
  ``output_text.done`` / ``content_part.done`` / annotation events, and
  ``max_output_tokens`` produces ``status: "incomplete"`` with
  ``response.incomplete``.
- **Web-chat artifacts**: images/videos/documents generated inside the turn are
  appended to the output text as Markdown links to ``/v1/media``.

Everything else (instructions framing, thread reuse, failover) reuses the chat
machinery via ``_build_plan`` / ``_retry_nonstream`` / ``_message_from_text``.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
import urllib.parse
import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict

from ...core.compaction import decode_compaction, encode_compaction
from ...core.media import load_image_ref
from ...core.models import resolve_model
from ...core.params import validate_metadata
from ...core.tokens import count_tokens, truncate_tokens
from ...drivers.base import ChatRequest, InputImage
from ...errors import InvalidRequest, Muse2APIError, NotFound
from ...services.container import Services
from ...services.conversation_store import items_to_messages, messages_to_items
from ..deps import conversation_key, get_services, public_base, require_api_key
from ..schemas import (
    ChatCompletionRequest,
    ChatMessage,
    ResponseCompactRequest,
)
from .chat import (
    _WEB_SEARCH_DIRECTIVE,
    _build_plan,
    _collect,
    _media_suffix,
    _message_from_text,
    _retry_nonstream,
)

log = logging.getLogger(__name__)
router = APIRouter(tags=["responses"], dependencies=[Depends(require_api_key)])

_COMPACT_DIRECTIVE = (
    "Compact the conversation into a self-contained state summary. Preserve "
    "all facts, decisions, identifiers, user preferences, constraints, "
    "unresolved tasks and tool results. Remove repetition and small talk. "
    "Reply with the summary only, in plain text; do not answer any question."
)


def _approx_tokens(text: str) -> int:
    return count_tokens(text)


def _conversation_id(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        return str(value.get("id") or "").strip() or None
    raise InvalidRequest(
        "conversation must be an id or an object with an 'id'",
        param="conversation")


def _sse(payload: dict | str) -> bytes:
    if isinstance(payload, str):
        return f"data: {payload}\n\n".encode()
    event_type = str(payload.get("type") or "message")
    data = json.dumps(payload, ensure_ascii=False)
    # OpenAI's Responses stream labels every event with an SSE ``event:`` line;
    # strict clients dispatch on it instead of parsing ``data.type``.
    return f"event: {event_type}\ndata: {data}\n\n".encode()


def _replay_events(response: dict[str, Any], starting_after: int | None = None):
    """Replay a stored response as the same SSE event sequence as the create call.

    ``GET /v1/responses/{id}?stream=true`` is used by the official SDK's
    ``responses.retrieve(..., stream=True)``; it needs a terminal event, not a
    snapshot object.
    """
    status = str(response.get("status") or "completed")
    created = dict(response)
    created["status"] = "in_progress"
    created["output"] = []
    created["usage"] = None
    created["completed_at"] = None

    seq = 0

    def emit(payload: dict[str, Any]) -> dict[str, Any]:
        nonlocal seq
        payload["sequence_number"] = seq
        seq += 1
        return payload

    events: list[dict[str, Any]] = [
        emit({"type": "response.created", "response": created}),
        emit({"type": "response.in_progress", "response": created}),
    ]
    for output_index, item in enumerate(response.get("output") or []):
        item_type = item.get("type")
        if item_type == "message":
            pending = {**item, "status": "in_progress", "content": []}
            events.append(emit({"type": "response.output_item.added",
                                "output_index": output_index, "item": pending}))
            for content_index, part in enumerate(item.get("content") or []):
                text = str(part.get("text") or "")
                annotations = part.get("annotations") or []
                events.append(emit({
                    "type": "response.content_part.added",
                    "item_id": item.get("id"), "output_index": output_index,
                    "content_index": content_index,
                    "part": {"type": "output_text", "text": "",
                             "annotations": [], "logprobs": []}}))
                events.append(emit({
                    "type": "response.output_text.delta",
                    "item_id": item.get("id"), "output_index": output_index,
                    "content_index": content_index,
                    "delta": text, "logprobs": []}))
                for annotation_index, annotation in enumerate(annotations):
                    events.append(emit({
                        "type": "response.output_text.annotation.added",
                        "item_id": item.get("id"), "output_index": output_index,
                        "content_index": content_index,
                        "annotation_index": annotation_index,
                        "annotation": annotation}))
                events.append(emit({
                    "type": "response.output_text.done",
                    "item_id": item.get("id"), "output_index": output_index,
                    "content_index": content_index, "text": text,
                    "logprobs": []}))
                events.append(emit({
                    "type": "response.content_part.done",
                    "item_id": item.get("id"), "output_index": output_index,
                    "content_index": content_index,
                    "part": {**part, "logprobs": []}}))
            events.append(emit({"type": "response.output_item.done",
                                "output_index": output_index, "item": item}))
        elif item_type == "function_call":
            pending = {**item, "status": "in_progress", "arguments": ""}
            events.append(emit({"type": "response.output_item.added",
                                "output_index": output_index, "item": pending}))
            arguments = str(item.get("arguments") or "")
            events.append(emit({
                "type": "response.function_call_arguments.delta",
                "item_id": item.get("id"), "output_index": output_index,
                "delta": arguments}))
            events.append(emit({
                "type": "response.function_call_arguments.done",
                "item_id": item.get("id"), "output_index": output_index,
                "arguments": arguments}))
            events.append(emit({"type": "response.output_item.done",
                                "output_index": output_index, "item": item}))
    terminal = {
        "completed": "response.completed",
        "incomplete": "response.incomplete",
        "failed": "response.failed",
        "cancelled": "response.cancelled",
    }.get(status, "response.completed")
    events.append(emit({"type": terminal, "response": response}))
    for event in events:
        if starting_after is not None and event["sequence_number"] <= starting_after:
            continue
        yield event


class ResponseRequest(BaseModel):
    model: str | None = None
    input: Any = None
    instructions: Any = None
    stream: bool = False
    user: str | None = None
    max_output_tokens: int | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: Any = None
    parallel_tool_calls: bool | None = None
    metadata: dict[str, Any] | None = None
    reasoning: dict[str, Any] | None = None
    truncation: str | None = None
    # Accepted for compatibility; muse.ai does not expose sampling controls.
    temperature: float | None = None
    top_p: float | None = None
    # OpenAI defaults to storing responses so later calls can reference them
    # via previous_response_id; store=false opts out.
    store: bool = True
    previous_response_id: str | None = None
    background: bool = False
    service_tier: str | None = None
    conversation: Any = None
    max_tool_calls: int | None = None
    top_logprobs: int | None = None
    safety_identifier: str | None = None
    prompt_cache_key: str | None = None
    prompt: Any = None
    # {"format": {...}} — mapped onto the chat machinery's response_format.
    text: dict[str, Any] | None = None

    model_config = ConfigDict(extra="allow")


# Item types the official SDK round-trips but muse.ai cannot fulfil. Reasoning
# and hosted-search results are outputs, not inputs: clients that echo a full
# transcript back can safely have them ignored.
_UNSUPPORTED_ITEMS = {"computer_call", "item_reference"}
_SKIPPABLE_ITEMS = {"reasoning", "file_search_call", "web_search_call",
                    "image_generation_call"}


def _text_format_to_response_format(text: dict[str, Any] | None) -> tuple[
        dict[str, Any] | None, dict[str, Any]]:
    """``text.format`` → (chat response_format, echoed format)."""
    echoed = {"type": "text"}
    if isinstance(text, dict) and isinstance(text.get("format"), dict):
        fmt = text["format"]
        echoed = fmt
        ftype = fmt.get("type")
        if ftype == "json_object":
            return {"type": "json_object"}, echoed
        if ftype == "json_schema":
            return ({"type": "json_schema",
                     "json_schema": {"name": fmt.get("name") or "response",
                                     "schema": fmt.get("schema") or {}}}, echoed)
    return None, echoed


def _to_messages(body: ResponseRequest) -> list[dict]:
    """Convert Responses ``input`` / ``instructions`` to chat-style messages."""
    messages: list[dict] = []
    if body.instructions:
        if isinstance(body.instructions, str):
            messages.append({"role": "system", "content": body.instructions})
        elif isinstance(body.instructions, list):
            # The newer API accepts an input-item list as instructions; only
            # text parts can be framed as a system turn.
            text = "\n".join(_text_parts(body.instructions))
            if text:
                messages.append({"role": "system", "content": text})
        else:
            raise InvalidRequest("instructions must be a string or a list",
                                 param="instructions")
    inp = body.input
    if inp is None or (isinstance(inp, str) and not inp.strip()):
        pass
    elif isinstance(inp, str):
        messages.append({"role": "user", "content": inp})
    elif isinstance(inp, list):
        for item in inp:
            if not isinstance(item, dict):
                raise InvalidRequest("input items must be objects", param="input")
            itype = item.get("type")
            if itype == "compaction":
                summary = decode_compaction(str(item.get("encrypted_content") or ""))
                if summary is None:
                    raise InvalidRequest(
                        "invalid compaction item; expected encrypted_content "
                        "from POST /v1/responses/compact",
                        param="input", code="invalid_compaction")
                messages.append({"role": "system",
                                 "content": f"Compacted conversation state:\n{summary}"})
                continue
            if itype in _SKIPPABLE_ITEMS:
                continue
            if itype in _UNSUPPORTED_ITEMS:
                raise InvalidRequest(
                    f"input item type '{itype}' is not supported: "
                    "muse.ai does not implement it", param="input")
            if itype == "function_call":
                messages.append({
                    "role": "assistant", "content": None,
                    "tool_calls": [{
                        "id": item.get("call_id") or item.get("id") or "call_" + uuid.uuid4().hex[:24],
                        "type": "function",
                        "function": {"name": item.get("name") or "",
                                     "arguments": item.get("arguments") or ""}}],
                })
            elif itype == "function_call_output":
                output = item.get("output")
                if isinstance(output, str):
                    content: Any = output
                elif isinstance(output, list):
                    # The Responses API allows text/image/file parts here; keep
                    # attachments instead of stringifying the whole list.
                    parts: list[dict] = []
                    for part in output:
                        if not isinstance(part, dict):
                            continue
                        if part.get("type") in ("input_text", "text", "output_text"):
                            parts.append({"type": "text",
                                          "text": part.get("text") or ""})
                        elif part.get("type") in ("input_image", "input_file"):
                            parts.append({k: v for k, v in part.items()
                                          if k not in ("id", "status")})
                    content = parts or ""
                else:
                    content = json.dumps(output, ensure_ascii=False)
                messages.append({"role": "tool",
                                 "tool_call_id": item.get("call_id") or "",
                                 "content": content})
            elif itype in ("input_image", "input_file", "input_audio"):
                # Standalone attachment items become a one-part message; the
                # chat flatten layer maps them onto file_ref attachments.
                part = {k: v for k, v in item.items()
                        if k not in ("id", "status")}
                messages.append({"role": item.get("role", "user"), "content": [part]})
            elif "role" in item or itype in ("message", None):
                messages.append({"role": item.get("role", "user"),
                                 "content": item.get("content")})
            else:
                raise InvalidRequest(f"unsupported input item type '{itype}'",
                                     param="input")
    else:
        raise InvalidRequest("input must be a string or a list of items",
                             param="input")
    return messages


async def _resolve_file_urls(messages: list[dict], svc: Services) -> None:
    """Turn ``input_file.file_url`` attachments into inline data URLs.

    The upstream attachment channel accepts bytes, not remote URLs, so the
    gateway fetches public URLs itself (SSRF-guarded by ``load_image_ref``).
    """
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict) or part.get("type") not in (
                    "file", "input_file"):
                continue
            nested = part.get("file") if isinstance(part.get("file"), dict) else None
            holder = nested or part
            url = holder.get("file_url") or part.get("file_url")
            if not url:
                continue
            try:
                data, mime = await load_image_ref(str(url), timeout=20.0)
            except Muse2APIError as exc:
                raise InvalidRequest(
                    f"file_url could not be fetched: {exc.message}",
                    param="input_file") from exc
            holder["file_data"] = (
                f"data:{mime};base64,{base64.b64encode(data).decode()}")
            if not holder.get("filename"):
                name = urllib.parse.urlparse(str(url)).path.rsplit("/", 1)[-1]
                if name:
                    holder["filename"] = name
            holder.pop("file_url", None)


def _message_item(msg_id: str, text: str, annotations: list[dict] | None = None) -> dict:
    return {"id": msg_id, "type": "message", "status": "completed", "role": "assistant",
            "content": [{"type": "output_text", "annotations": annotations or [],
                         "text": text, "logprobs": []}]}


def _function_call_item(call: dict) -> dict:
    return {"id": "fc_" + uuid.uuid4().hex[:24], "type": "function_call",
            "status": "completed", "call_id": call["id"],
            "name": call["function"]["name"],
            "arguments": call["function"]["arguments"]}


def _response_annotations(content: str, citations) -> list[dict]:
    """Responses ``output_text.annotations`` (flat url_citation shape)."""
    out: list[dict] = []
    seen: set[str] = set()
    for citation in citations or []:
        url = getattr(citation, "url", None) or (
            citation.get("url") if isinstance(citation, dict) else "")
        title = getattr(citation, "title", None) or (
            citation.get("title") if isinstance(citation, dict) else "")
        if not url or url in seen:
            continue
        seen.add(url)
        index = content.find(url)
        start = index if index >= 0 else len(content)
        end = start + len(url) if index >= 0 else start
        out.append({"type": "url_citation", "url": url, "title": title or url,
                    "start_index": start, "end_index": end})
    return out


def _usage(input_text: str, output_text: str) -> dict:
    i, o = _approx_tokens(input_text), _approx_tokens(output_text)
    return {"input_tokens": i, "output_tokens": o, "total_tokens": i + o,
            "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 0}}


def _text_parts(content: Any) -> list[str]:
    if content is None:
        return []
    if isinstance(content, str):
        return [content] if content else []
    if isinstance(content, list):
        out: list[str] = []
        for part in content:
            if isinstance(part, dict) and part.get("type") in (
                    "text", "input_text", "output_text"):
                text = str(part.get("text") or "")
                if text:
                    out.append(text)
        return out
    return [str(content)]


def _input_content(content: Any, *, assistant: bool = False) -> list[dict]:
    """Stored content → Responses message parts (text, images, files)."""
    text_type = "output_text" if assistant else "input_text"
    parts: list[dict] = []
    if isinstance(content, str):
        return [{"type": text_type, "text": content}] if content else []
    for part in content or []:
        if not isinstance(part, dict):
            continue
        ptype = part.get("type")
        if ptype in ("text", "input_text", "output_text"):
            text = str(part.get("text") or "")
            if text:
                item = {"type": text_type, "text": text}
                if assistant:
                    item["annotations"] = []
                parts.append(item)
        elif ptype in ("image_url", "input_image"):
            ref = part.get("image_url")
            url = ref.get("url") if isinstance(ref, dict) else ref
            item: dict[str, Any] = {"type": "input_image"}
            if part.get("file_id"):
                item["file_id"] = part["file_id"]
            elif url:
                item["image_url"] = url
            if part.get("detail"):
                item["detail"] = part["detail"]
            if "file_id" in item or "image_url" in item:
                parts.append(item)
        elif ptype in ("file", "input_file"):
            nested = part.get("file") if isinstance(part.get("file"), dict) else {}
            file_id = nested.get("file_id") or part.get("file_id")
            file_data = nested.get("file_data") or part.get("file_data")
            item = {"type": "input_file"}
            if file_id:
                item["file_id"] = file_id
            elif file_data:
                item["file_data"] = file_data
            filename = nested.get("filename") or part.get("filename")
            if filename:
                item["filename"] = filename
            if "file_id" in item or "file_data" in item:
                parts.append(item)
    return parts


def _input_items(response_id: str, messages: list[dict]) -> list[dict]:
    """Chat-style stored messages → OpenAI Responses input items."""
    items: list[dict] = []
    index = 0

    def item_id(prefix: str) -> str:
        nonlocal index
        index += 1
        return f"{prefix}_{response_id}_{index}"

    for message in messages:
        role = str(message.get("role") or "user")
        content = message.get("content")
        if role in ("system", "developer", "user"):
            parts = _input_content(content)
            if parts:
                items.append({"id": item_id("msg"), "type": "message",
                              "status": "completed", "role": role,
                              "content": parts})
        elif role == "assistant":
            for call in message.get("tool_calls") or []:
                fn = call.get("function") if isinstance(call, dict) else None
                if not isinstance(fn, dict):
                    continue
                items.append({"id": item_id("fc"), "type": "function_call",
                              "status": "completed",
                              "call_id": call.get("id") or item_id("call"),
                              "name": fn.get("name") or "",
                              "arguments": fn.get("arguments") or ""})
            parts = _input_content(content, assistant=True)
            if parts:
                items.append({"id": item_id("msg"), "type": "message",
                              "status": "completed", "role": "assistant",
                              "content": parts})
        elif role == "tool":
            items.append({"id": item_id("fco"), "type": "function_call_output",
                          "status": "completed",
                          "call_id": message.get("tool_call_id") or "",
                          "output": "\n".join(_text_parts(content))})
    return items


def _legacy_response(entry: dict[str, Any]) -> dict[str, Any]:
    """Rebuild a response object for entries saved before 0.11.0."""
    messages = entry.get("messages") or []
    text = ""
    for message in reversed(messages):
        if message.get("role") == "assistant" and isinstance(message.get("content"), str):
            text = message["content"]
            break
    response_id = str(entry.get("id") or "resp_legacy")
    return _response_object(
        response_id, int(entry.get("created", time.time())),
        str(entry.get("model") or "muse-chat"),
        output=[_message_item("msg_" + response_id, text)],
        input_text="", output_text=text, instructions=None,
        previous_response_id=None, store=True, tools=None,
        max_output_tokens=None, text_format={"type": "text"})


def _response_object(resp_id: str, created: int, model: str, *, status: str = "completed",
                     output: list[dict], input_text: str, output_text: str,
                     instructions: str | None, previous_response_id: str | None,
                     store: bool, tools: list[dict] | None,
                     max_output_tokens: int | None, text_format: dict,
                     incomplete: dict | None = None,
                     metadata: dict[str, Any] | None = None,
                     user: str | None = None,
                     parallel_tool_calls: bool | None = None,
                     tool_choice: Any = None,
                     reasoning: dict[str, Any] | None = None,
                     truncation: str | None = None,
                     temperature: float | None = None,
                     top_p: float | None = None,
                     background: bool = False,
                     service_tier: str = "default",
                     conversation: dict[str, Any] | None = None,
                     max_tool_calls: int | None = None,
                     top_logprobs: int | None = None,
                     safety_identifier: str | None = None,
                     prompt_cache_key: str | None = None,
                     completed_at: int | None = None,
                     error: dict[str, Any] | None = None) -> dict:
    effort = reasoning.get("effort") if isinstance(reasoning, dict) else None
    summary = reasoning.get("summary") if isinstance(reasoning, dict) else None
    context = reasoning.get("context") if isinstance(reasoning, dict) else None
    return {"id": resp_id, "object": "response", "created_at": created, "model": model,
            "status": status, "error": error, "incomplete_details": incomplete,
            "completed_at": completed_at if completed_at is not None
            else (int(time.time()) if status == "completed" else None),
            "instructions": instructions, "metadata": metadata or {},
            "output": output, "access_programs": None,
            "background": background,
            "service_tier": service_tier,
            "conversation": conversation,
            "max_tool_calls": max_tool_calls,
            "top_logprobs": top_logprobs,
            "safety_identifier": safety_identifier,
            "prompt_cache_key": prompt_cache_key,
            "parallel_tool_calls": True if parallel_tool_calls is None else parallel_tool_calls,
            "tool_choice": "auto" if tool_choice is None else tool_choice,
            "tools": tools or [],
            "temperature": temperature, "top_p": top_p,
            "max_output_tokens": max_output_tokens,
            "previous_response_id": previous_response_id,
            "reasoning": {"effort": effort, "summary": summary, "context": context},
            "store": store, "text": {"format": text_format},
            "truncation": truncation or "disabled",
            "usage": _usage(input_text, output_text)
            if status in ("completed", "incomplete") else None,
            "user": user}


@router.post("/v1/responses")
async def create_response(body: ResponseRequest, request: Request,
                          svc: Services = Depends(get_services)):
    spec = resolve_model(body.model, "chat")
    request.state.model = spec.id  # log the actual model, not the client alias
    if body.background:
        # The upstream protocol has no resumable server-side queue: a turn is
        # bound to the websocket that started it. Reject instead of returning a
        # "queued" object that would never progress.
        raise InvalidRequest(
            "background responses are not supported by this gateway; "
            "use a normal request and poll GET /v1/responses/{id}",
            param="background")
    if body.prompt is not None:
        raise InvalidRequest(
            "prompt templates are not supported; inline the prompt text in "
            "instructions", param="prompt")
    if body.service_tier not in (None, "auto", "default", "flex",
                                     "scale", "priority", "fast"):
        raise InvalidRequest(
            f"service_tier '{body.service_tier}' is not available for muse models",
            param="service_tier")
    if body.top_logprobs:
        raise InvalidRequest(
            "log probabilities are not supported by muse models",
            param="top_logprobs")
    body.metadata = validate_metadata(body.metadata)
    owner = getattr(request.state, "key_id", None)

    own_messages = _to_messages(body)
    # ``input_items`` returns the request's input items; instructions are a
    # separate field upstream, not an input message.
    input_source = own_messages
    if body.instructions:
        input_source = _to_messages(body.model_copy(update={"instructions": None}))
    conversation_id = _conversation_id(body.conversation)
    if body.conversation is not None:
        if body.previous_response_id:
            raise InvalidRequest(
                "conversation cannot be combined with previous_response_id",
                param="conversation")
        if not conversation_id:
            raise InvalidRequest(
                "conversation must be an id or an object with an 'id'",
                param="conversation")
        conversation_entry = svc.conversations.entry(conversation_id, owner)
        if conversation_entry is None:
            raise NotFound(
                f"No conversation found with id '{conversation_id}'.",
                param="conversation", code="conversation_not_found")
        own_messages = (items_to_messages(conversation_entry.get("items") or [])
                        + own_messages)
    chain_hint = None
    if body.previous_response_id:
        entry = svc.responses.get(body.previous_response_id, owner)
        if entry is None:
            raise NotFound(
                f"No response found with id '{body.previous_response_id}'.",
                param="previous_response_id")
        stored = entry.get("messages") or []
        # OpenAI semantics: instructions from the previous response are never
        # carried over; each request's own instructions (or none) apply.
        stored = [m for m in stored if m.get("role") not in ("system", "developer")]
        own_messages = stored + own_messages
        chain_hint = entry.get("hint")

    if not own_messages:
        raise InvalidRequest("input contains no text or images", param="input")
    await _resolve_file_urls(own_messages, svc)

    # Responses fields mapped onto the chat planner: tools/tool_choice, and
    # text.format → response_format; max_output_tokens → max_tokens (the cap).
    response_format, text_format = _text_format_to_response_format(body.text)
    plan_messages = svc.files.expand_messages(own_messages, owner)
    chat_body = ChatCompletionRequest(
        model=body.model,
        messages=[ChatMessage(role=m["role"], content=m.get("content"))
                  for m in plan_messages],
        tools=body.tools or None,
        tool_choice=body.tool_choice,
        parallel_tool_calls=body.parallel_tool_calls,
        max_completion_tokens=body.max_output_tokens,
        response_format=response_format,
    )
    plan = _build_plan(chat_body, plan_messages, svc.settings)
    if not plan.prompt and not plan.images:
        raise InvalidRequest("input contains no text or images", param="input")
    if plan.json_directive:
        plan.prompt = f"{plan.json_directive}\n\n{plan.prompt}"
        if plan.followup_prompt:
            plan.followup_prompt = f"{plan.json_directive}\n\n{plan.followup_prompt}"
    if plan.web_search:
        plan.prompt = f"{_WEB_SEARCH_DIRECTIVE}\n\n{plan.prompt}"
        if plan.followup_prompt:
            plan.followup_prompt = f"{_WEB_SEARCH_DIRECTIVE}\n\n{plan.followup_prompt}"

    images = [InputImage(*(await load_image_ref(ref))) for ref in plan.images]
    cancel = asyncio.Event()
    hint = chain_hint or conversation_key(request, body.user)
    req = ChatRequest(
        prompt=plan.prompt,
        model=spec.id,
        images=images,
        timeout=svc.settings.chat_timeout,
        first_token_timeout=svc.settings.first_token_timeout,
        conversation_hint=hint,
        turns=plan.turns,
        followup_prompt=plan.followup_prompt,
        tool_continuation=plan.tool_continuation,
        images_current_only=plan.images_current_only,
        system_fingerprint=plan.system_fp,
        page_echo=(plan.current_user_text or "")[:200] or None,
        tools=plan.tools,
        system_prompt=plan.system_prompt or None,
        cancel=cancel,
    )
    stream = svc.gateway.chat_stream(req)
    resp_id = "resp_" + uuid.uuid4().hex[:24]
    msg_id = "msg_" + uuid.uuid4().hex[:24]
    created = int(time.time())
    model_name = body.model or spec.id
    buffered = bool(plan.tools) or bool(plan.json_directive)
    input_text = plan.prompt
    base = public_base(request)
    conversation = (svc.conversations.get(conversation_id, owner)
                    if conversation_id else None)
    echo = dict(metadata=body.metadata, user=body.user,
                parallel_tool_calls=body.parallel_tool_calls,
                tool_choice=body.tool_choice, reasoning=body.reasoning,
                truncation=body.truncation, temperature=body.temperature,
                top_p=body.top_p, background=body.background,
                service_tier="default",
                conversation=conversation, max_tool_calls=body.max_tool_calls,
                top_logprobs=body.top_logprobs,
                safety_identifier=body.safety_identifier,
                prompt_cache_key=body.prompt_cache_key)

    conversation_recorded = False

    def save_chain(final_message: dict, response: dict | None = None) -> None:
        """Record this response's chat messages so later calls can continue it."""
        nonlocal conversation_recorded
        if (conversation_id and not conversation_recorded
                and not cancel.is_set()):
            status = (response or {}).get("status")
            if status in ("completed", "incomplete"):
                try:
                    new_items = (messages_to_items(input_source)
                                 + messages_to_items([final_message]))
                    if svc.conversations.add_items(
                            conversation_id, new_items, owner) is not None:
                        conversation_recorded = True
                except Exception:  # noqa: BLE001 - conversation must not lose the reply
                    log.exception("failed to append to conversation %s",
                                  conversation_id)
        if body.store:
            if cancel.is_set():
                # cancel_response already persisted the cancelled object; a
                # partial stream finishing must not rewrite it as completed.
                entry = svc.responses.get(resp_id, owner) or {}
                if (entry.get("response") or {}).get("status") == "cancelled":
                    return
            try:
                svc.responses.save(resp_id, model=model_name,
                                   messages=own_messages + [final_message], hint=hint,
                                   response=response, input_messages=input_source, owner=owner)
            except OSError:
                log.exception("failed to persist response %s", resp_id)

    def fail_chain(exc: Muse2APIError) -> None:
        if not body.store:
            return
        if exc.code == "upstream_quota_exhausted":
            error_code = "rate_limit_exceeded"
        elif exc.code == "invalid_request":
            error_code = "invalid_prompt"
        else:
            error_code = "server_error"
        try:
            failed = _response_object(
                resp_id, created, model_name, status="failed", output=[],
                input_text=input_text, output_text="",
                instructions=body.instructions,
                previous_response_id=body.previous_response_id,
                store=body.store, tools=body.tools,
                max_output_tokens=body.max_output_tokens,
                text_format=text_format,
                error={"code": error_code, "message": exc.message}, **echo)
            save_chain({"role": "assistant", "content": ""}, failed)
        except OSError:
            log.exception("failed to persist failed response %s", resp_id)

    def fail_chain_unexpected() -> None:
        fail_chain(Muse2APIError("internal error", code="internal_error"))

    snapshot = _response_object(
        resp_id, created, model_name, status="in_progress", output=[],
        input_text=input_text, output_text="",
        instructions=body.instructions,
        previous_response_id=body.previous_response_id,
        store=body.store, tools=body.tools,
        max_output_tokens=body.max_output_tokens,
        text_format=text_format, **echo)
    svc.responses.start_live(resp_id, cancel, snapshot, owner=owner)
    if body.store:
        # Make the response retrievable while it is still running (OpenAI does
        # this too); save_chain/save_cancelled overwrite it when it settles.
        svc.responses.save(resp_id, model=model_name, messages=own_messages,
                           hint=hint, response=snapshot, input_messages=input_source, owner=owner)

    if not body.stream:
        try:
            text = await _collect(stream)
            if cancel.is_set():
                cancelled = _response_object(
                    resp_id, created, model_name, status="cancelled", output=[],
                    input_text=input_text, output_text="",
                    instructions=body.instructions,
                    previous_response_id=body.previous_response_id,
                    store=body.store, tools=body.tools,
                    max_output_tokens=body.max_output_tokens,
                    text_format=text_format, **echo)
                save_chain({"role": "assistant", "content": ""}, cancelled)
                return cancelled
            text = await _retry_nonstream(text, plan, req, svc, plan_messages)
            message, finish = _message_from_text(plan, text)
            suffix = "" if plan.json_directive else _media_suffix(req.media, svc, base)
            if message.get("tool_calls"):
                output = [_function_call_item(call) for call in message["tool_calls"]]
                usage_text = json.dumps(message["tool_calls"], ensure_ascii=False)
            else:
                model_text = message.get("content") or ""
                content = model_text + suffix
                annotations = _response_annotations(content, req.citations)
                output = [_message_item(msg_id, content, annotations)]
                usage_text = model_text
            incomplete = {"reason": "max_output_tokens"} if finish == "length" else None
            result = _response_object(
                resp_id, created, model_name,
                status="incomplete" if incomplete else "completed",
                output=output, input_text=input_text, output_text=usage_text,
                instructions=body.instructions,
                previous_response_id=body.previous_response_id,
                store=body.store, tools=body.tools,
                max_output_tokens=body.max_output_tokens,
                text_format=text_format, incomplete=incomplete, **echo)
            save_chain(message, result)
            return result
        except Muse2APIError as exc:
            fail_chain(exc)
            raise
        except Exception:
            fail_chain_unexpected()
            raise
        finally:
            svc.responses.finish_live(resp_id)

    # --- streaming -----------------------------------------------------------

    if buffered:
        # Planner JSON must be rewritten into function_call items (or JSON mode
        # extracted) before the text events go out. The response lifecycle
        # events are emitted first so the client sees a 200 immediately and can
        # keep its timeout alive while the tool turn runs.
        async def buffered_events() -> AsyncIterator[bytes]:
            seq = 0
            try:
                yield _sse({"type": "response.created", "sequence_number": seq,
                            "response": _response_object(
                                resp_id, created, model_name, status="in_progress",
                                output=[], input_text=input_text, output_text="",
                                instructions=body.instructions,
                                previous_response_id=body.previous_response_id,
                                store=body.store, tools=body.tools,
                                max_output_tokens=body.max_output_tokens,
                                text_format=text_format, **echo)})
                seq += 1
                yield _sse({"type": "response.in_progress", "sequence_number": seq,
                            "response": _response_object(
                                resp_id, created, model_name, status="in_progress",
                                output=[], input_text=input_text, output_text="",
                                instructions=body.instructions,
                                previous_response_id=body.previous_response_id,
                                store=body.store, tools=body.tools,
                                max_output_tokens=body.max_output_tokens,
                                text_format=text_format, **echo)})
                seq += 1
                # The tool turn can take a minute; emit SSE comments so the
                # client's read timeout keeps resetting while we buffer.
                pending = asyncio.ensure_future(_collect(stream))
                try:
                    while not pending.done():
                        done, _ = await asyncio.wait({pending}, timeout=15)
                        if not done:
                            yield b": keepalive\n\n"
                    text = pending.result()
                finally:
                    if not pending.done():
                        pending.cancel()
                text = await _retry_nonstream(text, plan, req, svc, plan_messages)
                message, _finish = _message_from_text(plan, text)
                buffered_content = (message.get("content") or "") + (
                "" if plan.json_directive else _media_suffix(req.media, svc, base))
                buffered_annotations = _response_annotations(buffered_content,
                                                             req.citations)
                if message.get("tool_calls"):
                    items = [_function_call_item(call) for call in message["tool_calls"]]
                    for index, item in enumerate(items):
                        added = {**item, "status": "in_progress", "arguments": ""}
                        yield _sse({"type": "response.output_item.added",
                                    "sequence_number": seq, "output_index": index,
                                    "item": added})
                        seq += 1
                        yield _sse({"type": "response.function_call_arguments.delta",
                                    "sequence_number": seq, "item_id": item["id"],
                                    "output_index": index, "delta": item["arguments"]})
                        seq += 1
                        yield _sse({"type": "response.function_call_arguments.done",
                                    "sequence_number": seq, "item_id": item["id"],
                                    "output_index": index,
                                    "arguments": item["arguments"]})
                        seq += 1
                        yield _sse({"type": "response.output_item.done",
                                    "sequence_number": seq, "output_index": index,
                                    "item": item})
                        seq += 1
                else:
                    content = buffered_content
                    yield _sse({"type": "response.output_item.added",
                                "sequence_number": seq, "output_index": 0,
                                "item": {"id": msg_id, "type": "message",
                                         "status": "in_progress", "role": "assistant",
                                         "content": []}})
                    seq += 1
                    yield _sse({"type": "response.content_part.added",
                                "sequence_number": seq, "item_id": msg_id,
                                "output_index": 0, "content_index": 0,
                                "part": {"type": "output_text", "text": "",
                                         "annotations": [], "logprobs": []}})
                    seq += 1
                    yield _sse({"type": "response.output_text.delta",
                                "sequence_number": seq, "item_id": msg_id,
                                "output_index": 0, "content_index": 0,
                                "delta": content, "logprobs": []})
                    seq += 1
                    for annotation_index, annotation in enumerate(buffered_annotations):
                        yield _sse({"type": "response.output_text.annotation.added",
                                    "sequence_number": seq, "item_id": msg_id,
                                    "output_index": 0, "content_index": 0,
                                    "annotation_index": annotation_index,
                                    "annotation": annotation})
                        seq += 1
                    yield _sse({"type": "response.output_text.done",
                                "sequence_number": seq, "item_id": msg_id,
                                "output_index": 0, "content_index": 0,
                                "text": content, "logprobs": []})
                    seq += 1
                    yield _sse({"type": "response.content_part.done",
                                "sequence_number": seq, "item_id": msg_id,
                                "output_index": 0, "content_index": 0,
                                "part": {"type": "output_text", "text": content,
                                         "annotations": buffered_annotations,
                                         "logprobs": []}})
                    seq += 1
                    yield _sse({"type": "response.output_item.done",
                                "sequence_number": seq, "output_index": 0,
                                "item": _message_item(msg_id, content,
                                                      buffered_annotations)})
                    seq += 1
                completed = _response_object(
                    resp_id, created, model_name, output=items if message.get("tool_calls")
                    else [_message_item(msg_id, buffered_content, buffered_annotations)],
                    input_text=input_text,
                    output_text=message.get("content") or
                    json.dumps(message.get("tool_calls") or [], ensure_ascii=False),
                    instructions=body.instructions,
                    previous_response_id=body.previous_response_id,
                    store=body.store, tools=body.tools,
                    max_output_tokens=body.max_output_tokens,
                    text_format=text_format, **echo)
                yield _sse({"type": "response.completed", "sequence_number": seq,
                            "response": completed})
                save_chain(message, completed)
            except Muse2APIError as exc:
                fail_chain(exc)
                yield _sse({"type": "error", "sequence_number": seq,
                            "code": exc.code, "message": exc.message,
                            "param": exc.param})
            except Exception:  # noqa: BLE001
                log.exception("responses stream crashed")
                fail_chain_unexpected()
                yield _sse({"type": "error", "sequence_number": seq,
                            "code": "internal_error", "message": "internal error",
                            "param": None})
            finally:
                cancel.set()
                svc.responses.finish_live(resp_id)
                await stream.aclose()

        return StreamingResponse(buffered_events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    async def events(first: str | None) -> AsyncIterator[bytes]:
        # ``first`` is the pre-fetched first delta when heartbeats are disabled;
        # when enabled it is None and the first token is awaited here, after the
        # 200 (headers) has already been committed to the client.
        seq = 0
        produced = ""
        try:
            if first is None and svc.settings.stream_heartbeat_interval > 0:
                # Same C4 pattern as chat completions: send SSE comment
                # heartbeats while the gateway waits for the first token.
                pending = asyncio.ensure_future(anext(stream))
                try:
                    while True:
                        done, _ = await asyncio.wait(
                            {pending},
                            timeout=svc.settings.stream_heartbeat_interval)
                        if done:
                            try:
                                first = pending.result()
                            except StopAsyncIteration:
                                first = ""
                            break
                        yield b": keepalive\n\n"
                finally:
                    if not pending.done():
                        pending.cancel()
            yield _sse({"type": "response.created", "sequence_number": seq,
                        "response": _response_object(
                            resp_id, created, model_name, status="in_progress",
                            output=[], input_text=input_text, output_text="",
                            instructions=body.instructions,
                            previous_response_id=body.previous_response_id,
                            store=body.store, tools=body.tools,
                            max_output_tokens=body.max_output_tokens,
                            text_format=text_format, **echo)})
            seq += 1
            # The official SDK builds its accumulated snapshot from item/part
            # lifecycle events, so emit them around the text deltas.
            yield _sse({"type": "response.in_progress", "sequence_number": seq,
                        "response": _response_object(
                            resp_id, created, model_name, status="in_progress",
                            output=[], input_text=input_text, output_text="",
                            instructions=body.instructions,
                            previous_response_id=body.previous_response_id,
                            store=body.store, tools=body.tools,
                            max_output_tokens=body.max_output_tokens,
                            text_format=text_format, **echo)})
            seq += 1
            yield _sse({"type": "response.output_item.added", "sequence_number": seq,
                        "output_index": 0,
                        "item": {"id": msg_id, "type": "message", "status": "in_progress",
                                 "role": "assistant", "content": []}})
            seq += 1
            yield _sse({"type": "response.content_part.added", "sequence_number": seq,
                        "item_id": msg_id, "output_index": 0, "content_index": 0,
                        "part": {"type": "output_text", "text": "", "annotations": [],
                                 "logprobs": []}})
            seq += 1
            produced = first or ""

            def delta_event(delta: str) -> dict:
                return {"type": "response.output_text.delta", "sequence_number": seq,
                        "item_id": msg_id, "output_index": 0, "content_index": 0,
                        "delta": delta, "logprobs": []}

            hit_cap = False
            if plan.max_chars is not None and count_tokens(produced) > plan.max_chars:
                produced = truncate_tokens(produced, plan.max_chars)
                hit_cap = True
            elif produced:
                yield _sse(delta_event(produced))
                seq += 1
                async for delta in stream:
                    produced += delta
                    if plan.max_chars is not None and count_tokens(produced) > plan.max_chars:
                        # OpenAI marks the response incomplete; the tail beyond
                        # the cap is dropped and the upstream turn cancelled.
                        already = len(produced) - len(delta)
                        truncated = truncate_tokens(produced, plan.max_chars)
                        tail = truncated[already:] if len(truncated) > already else ""
                        if tail:
                            yield _sse(delta_event(tail))
                            seq += 1
                        produced = truncated
                        hit_cap = True
                        break
                    yield _sse(delta_event(delta))
                    seq += 1
            if hit_cap:
                cancel.set()
            model_text = produced
            suffix = "" if plan.json_directive else _media_suffix(req.media, svc, base)
            if suffix:
                produced += suffix
                yield _sse(delta_event(suffix))
                seq += 1
            annotations = _response_annotations(produced, req.citations)
            for annotation_index, annotation in enumerate(annotations):
                yield _sse({"type": "response.output_text.annotation.added",
                            "sequence_number": seq, "item_id": msg_id,
                            "output_index": 0, "content_index": 0,
                            "annotation_index": annotation_index,
                            "annotation": annotation})
                seq += 1
            yield _sse({"type": "response.output_text.done", "sequence_number": seq,
                        "item_id": msg_id, "output_index": 0, "content_index": 0,
                        "text": produced, "logprobs": []})
            seq += 1
            yield _sse({"type": "response.content_part.done", "sequence_number": seq,
                        "item_id": msg_id, "output_index": 0, "content_index": 0,
                        "part": {"type": "output_text", "text": produced,
                                 "annotations": annotations, "logprobs": []}})
            seq += 1
            yield _sse({"type": "response.output_item.done", "sequence_number": seq,
                        "output_index": 0,
                        "item": _message_item(msg_id, produced, annotations)})
            seq += 1
            incomplete = {"reason": "max_output_tokens"} if hit_cap else None
            final = _response_object(
                resp_id, created, model_name,
                status="incomplete" if incomplete else "completed",
                output=[_message_item(msg_id, produced, annotations)],
                input_text=input_text, output_text=model_text,
                instructions=body.instructions,
                previous_response_id=body.previous_response_id,
                store=body.store, tools=body.tools,
                max_output_tokens=body.max_output_tokens,
                text_format=text_format, incomplete=incomplete, **echo)
            yield _sse({"type": "response.completed" if not incomplete
                        else "response.incomplete",
                        "sequence_number": seq, "response": final})
            save_chain({"role": "assistant", "content": model_text}, final)
        except Muse2APIError as exc:
            fail_chain(exc)
            yield _sse({"type": "error", "sequence_number": seq,
                        "code": exc.code, "message": exc.message,
                        "param": exc.param})
        except Exception:  # noqa: BLE001
            log.exception("responses stream crashed")
            fail_chain_unexpected()
            yield _sse({"type": "error", "sequence_number": seq,
                        "code": "internal_error", "message": "internal error",
                        "param": None})
        finally:
            cancel.set()
            svc.responses.finish_live(resp_id)
            await stream.aclose()

    # With stream_heartbeat_interval set (C4), the 200 goes out immediately;
    # upstream errors then surface as SSE error events instead of HTTP codes.
    if svc.settings.stream_heartbeat_interval > 0:
        return StreamingResponse(events(None), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    # Default: pull the first delta before committing to a 200 so that
    # account/upstream failures surface as proper HTTP errors instead of a
    # broken event stream.
    try:
        first = await anext(stream)
    except StopAsyncIteration:
        first = ""

    return StreamingResponse(events(first), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@router.post("/v1/responses/compact")
async def compact_response(body: ResponseCompactRequest, request: Request,
                           svc: Services = Depends(get_services)) -> dict:
    """Compact a conversation into an opaque state item (gateway-local)."""
    spec = resolve_model(body.model, "chat")
    request.state.model = spec.id
    if body.service_tier not in (None, "auto", "default", "flex",
                                     "scale", "priority", "fast"):
        raise InvalidRequest(
            f"service_tier '{body.service_tier}' is not available for muse models",
            param="service_tier")
    owner = getattr(request.state, "key_id", None)
    shim = ResponseRequest(input=body.input, instructions=body.instructions,
                           model=body.model,
                           previous_response_id=body.previous_response_id)
    messages = _to_messages(shim)
    if body.previous_response_id:
        entry = svc.responses.get(body.previous_response_id, owner)
        if entry is None:
            raise NotFound(
                f"No response found with id '{body.previous_response_id}'.",
                param="previous_response_id")
        stored = entry.get("messages") or []
        stored = [m for m in stored
                  if m.get("role") not in ("system", "developer")]
        messages = stored + messages
    if not messages:
        raise InvalidRequest("input contains no text or images", param="input")
    await _resolve_file_urls(messages, svc)
    messages = [{"role": "system", "content": _COMPACT_DIRECTIVE}] + messages
    plan_messages = svc.files.expand_messages(messages, owner)
    chat_body = ChatCompletionRequest(
        model=body.model,
        messages=[ChatMessage(role=m["role"], content=m.get("content"))
                  for m in plan_messages],
        max_completion_tokens=2048,
    )
    plan = _build_plan(chat_body, plan_messages, svc.settings)
    if not plan.prompt and not plan.images:
        raise InvalidRequest("input contains no text or images", param="input")
    images = [InputImage(*(await load_image_ref(ref))) for ref in plan.images]
    request_obj = ChatRequest(
        prompt=plan.prompt, model=spec.id, images=images,
        timeout=svc.settings.chat_timeout,
        first_token_timeout=svc.settings.first_token_timeout,
        conversation_hint=conversation_key(request, None),
        turns=plan.turns, followup_prompt=plan.followup_prompt,
        tool_continuation=plan.tool_continuation,
        images_current_only=plan.images_current_only,
        system_fingerprint=plan.system_fp,
        page_echo=(plan.current_user_text or "")[:200] or None,
        tools=plan.tools, system_prompt=plan.system_prompt or None,
        cancel=asyncio.Event(),
    )
    text = await _collect(svc.gateway.chat_stream(request_obj))
    text = await _retry_nonstream(text, plan, request_obj, svc, plan_messages)
    message, _finish = _message_from_text(plan, text)
    summary = str(message.get("content") or text).strip()
    if not summary:
        raise InvalidRequest("compaction produced no summary",
                             code="empty_compaction")
    item = {
        "id": "cmp_" + uuid.uuid4().hex[:24],
        "type": "compaction",
        "encrypted_content": encode_compaction(summary),
    }
    return {
        "id": "resp_" + uuid.uuid4().hex[:24],
        "object": "response.compaction",
        "created_at": int(time.time()),
        "output": [item],
        "usage": _usage(plan.prompt, summary),
    }


@router.post("/v1/responses/input_tokens")
async def count_input_tokens(body: ResponseRequest, request: Request,
                             svc: Services = Depends(get_services)) -> dict:
    """Count the input tokens a create call would use, without generating."""
    owner = getattr(request.state, "key_id", None)
    messages = _to_messages(body)
    if body.conversation is not None:
        if body.previous_response_id:
            raise InvalidRequest(
                "conversation cannot be combined with previous_response_id",
                param="conversation")
        conversation_id = _conversation_id(body.conversation)
        if not conversation_id:
            raise InvalidRequest(
                "conversation must be an id or an object with an 'id'",
                param="conversation")
        conversation = svc.conversations.entry(conversation_id, owner)
        if conversation is None:
            raise NotFound(
                f"No conversation found with id '{conversation_id}'.",
                param="conversation", code="conversation_not_found")
        messages = items_to_messages(conversation.get("items") or []) + messages
    if body.previous_response_id:
        entry = svc.responses.get(body.previous_response_id, owner)
        if entry is None:
            raise NotFound(
                f"No response found with id '{body.previous_response_id}'.",
                param="previous_response_id")
        stored = entry.get("messages") or []
        stored = [m for m in stored if m.get("role") not in ("system", "developer")]
        messages = stored + messages
    if not messages:
        raise InvalidRequest("input contains no text or images", param="input")
    await _resolve_file_urls(messages, svc)
    expanded = svc.files.expand_messages(messages, owner)
    chat_body = ChatCompletionRequest(
        model=body.model,
        messages=[ChatMessage(role=m["role"], content=m.get("content"))
                  for m in expanded],
        tools=body.tools or None,
        tool_choice=body.tool_choice,
        parallel_tool_calls=body.parallel_tool_calls,
        max_completion_tokens=body.max_output_tokens,
    )
    plan = _build_plan(chat_body, expanded, svc.settings)
    prompt = plan.prompt
    if plan.json_directive:
        prompt = f"{plan.json_directive}\n\n{prompt}"
    if plan.web_search:
        prompt = f"{_WEB_SEARCH_DIRECTIVE}\n\n{prompt}"
    return {"object": "response.input_tokens",
            "input_tokens": _approx_tokens(prompt)}


@router.get("/v1/responses/{response_id}")
async def retrieve_response(
    response_id: str,
    request: Request,
    stream: bool = Query(default=False),
    starting_after: int | None = Query(default=None),
    include: list[str] | None = Query(default=None),
    svc: Services = Depends(get_services),
):
    owner = getattr(request.state, "key_id", None)
    entry = svc.responses.get(response_id, owner)
    if entry is None:
        raise NotFound(f"No response found with id '{response_id}'.",
                       param="response_id")
    include = include or []
    if any(item == "message.output_text.logprobs" for item in include):
        raise InvalidRequest(
            "logprobs are not available for muse responses", param="include")
    response = entry.get("response") or _legacy_response(entry)
    if not stream:
        return response
    if response.get("status") in ("queued", "in_progress"):
        raise InvalidRequest(
            "streaming an in-progress response is not supported; "
            "wait for it to complete", param="stream")

    async def replay() -> AsyncIterator[bytes]:
        for event in _replay_events(response, starting_after):
            yield _sse(event)

    return StreamingResponse(replay(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@router.delete("/v1/responses/{response_id}")
async def delete_response(response_id: str, request: Request,
                          svc: Services = Depends(get_services)) -> dict:
    owner = getattr(request.state, "key_id", None)
    # Stop a running turn first so it cannot keep consuming upstream quota.
    svc.responses.cancel_live(response_id, owner)
    if not svc.responses.delete(response_id, owner):
        raise NotFound(f"No response found with id '{response_id}'.",
                       param="response_id")
    return {"id": response_id, "object": "response.deleted", "deleted": True}


@router.post("/v1/responses/{response_id}/cancel")
async def cancel_response(response_id: str, request: Request,
                          svc: Services = Depends(get_services)) -> dict:
    owner = getattr(request.state, "key_id", None)
    entry = svc.responses.get(response_id, owner)
    snapshot = svc.responses.cancel_live(response_id, owner)
    if snapshot is None:
        if entry is None:
            raise NotFound(f"No response found with id '{response_id}'.",
                           param="response_id")
        raise InvalidRequest(
            f"Response '{response_id}' is not in progress and cannot be cancelled.",
            param="response_id")
    if entry is not None:
        # Persist the cancelled object so a later GET does not show in_progress.
        svc.responses.save(response_id, model=entry.get("model") or "muse-chat",
                           messages=entry.get("messages") or [],
                           hint=entry.get("hint"), response=snapshot,
                           input_messages=entry.get("input_messages"),
                           owner=owner)
    return snapshot


@router.get("/v1/responses/{response_id}/input_items")
async def list_input_items(
    response_id: str,
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    order: str = Query(default="desc", pattern="^(asc|desc)$"),
    after: str | None = Query(default=None),
    svc: Services = Depends(get_services),
) -> dict:
    entry = svc.responses.get(response_id, getattr(request.state, "key_id", None))
    if entry is None:
        raise NotFound(f"No response found with id '{response_id}'.",
                       param="response_id")
    items = _input_items(response_id,
                         entry.get("input_messages") or entry.get("messages") or [])
    if order == "desc":
        items = list(reversed(items))
    if after:
        ids = [item["id"] for item in items]
        if after in ids:
            items = items[ids.index(after) + 1:]
    has_more = len(items) > limit
    page = items[:limit]
    return {"object": "list", "data": page,
            "first_id": page[0]["id"] if page else None,
            "last_id": page[-1]["id"] if page else None,
            "has_more": has_more}
