"""OpenAI Responses API (``/v1/responses``).

0.10.0 brings this route to real API parity:

- **Stateful**: ``previous_response_id`` continues a stored conversation
  (``data/responses.json``, TTL ``MUSE2API_RESPONSE_TTL``); ``store`` controls
  whether the response is storable at all.
- **Function calling**: request ``tools`` / ``tool_choice`` ride the same
  planner as chat completions; ``function_call`` / ``function_call_output``
  input items are accepted and answers come back as ``function_call`` output
  items (streaming emits ``response.function_call_arguments.*``).
- **Complete event stream**: ``output_text.done`` and ``content_part.done`` are
  emitted between the deltas and ``output_item.done``, and ``max_output_tokens``
  produces ``status: "incomplete"`` with ``response.incomplete``.

Everything else (instructions framing, thread reuse, failover) reuses the chat
machinery via ``_build_plan`` / ``_retry_nonstream`` / ``_message_from_text``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict

from ...core.media import load_image_ref
from ...core.models import resolve_model
from ...drivers.base import ChatRequest, InputImage
from ...errors import InvalidRequest, Muse2APIError, NotFound
from ...services.container import Services
from ..deps import conversation_key, get_services, require_api_key
from ..schemas import ChatCompletionRequest, ChatMessage
from .chat import _build_plan, _collect, _message_from_text, _retry_nonstream

log = logging.getLogger(__name__)
router = APIRouter(tags=["responses"], dependencies=[Depends(require_api_key)])


def _approx_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _sse(payload: dict | str) -> bytes:
    data = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return f"data: {data}\n\n".encode()


class ResponseRequest(BaseModel):
    model: str | None = None
    input: Any = None
    instructions: str | None = None
    stream: bool = False
    user: str | None = None
    max_output_tokens: int | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: Any = None
    # OpenAI defaults to storing responses so later calls can reference them
    # via previous_response_id; store=false opts out.
    store: bool = True
    previous_response_id: str | None = None
    # {"format": {...}} — mapped onto the chat machinery's response_format.
    text: dict[str, Any] | None = None
    # Accepted for compatibility; muse.ai does not expose sampling controls.
    temperature: float | None = None
    top_p: float | None = None

    model_config = ConfigDict(extra="allow")


# Item types the official SDK round-trips but muse.ai cannot fulfil. Function
# calling IS supported since 0.10.0 (function_call / function_call_output).
_UNSUPPORTED_ITEMS = {"reasoning", "computer_call", "file_search_call",
                      "web_search_call", "image_generation_call", "item_reference"}


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
        messages.append({"role": "system", "content": body.instructions})
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
                messages.append({"role": "tool",
                                 "tool_call_id": item.get("call_id") or "",
                                 "content": output if isinstance(output, str)
                                 else json.dumps(output, ensure_ascii=False)})
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


def _message_item(msg_id: str, text: str) -> dict:
    return {"id": msg_id, "type": "message", "status": "completed", "role": "assistant",
            "content": [{"type": "output_text", "annotations": [], "text": text}]}


def _function_call_item(call: dict) -> dict:
    return {"id": "fc_" + uuid.uuid4().hex[:24], "type": "function_call",
            "status": "completed", "call_id": call["id"],
            "name": call["function"]["name"],
            "arguments": call["function"]["arguments"]}


def _usage(input_text: str, output_text: str) -> dict:
    i, o = _approx_tokens(input_text), _approx_tokens(output_text)
    return {"input_tokens": i, "output_tokens": o, "total_tokens": i + o,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 0}}


def _response_object(resp_id: str, created: int, model: str, *, status: str = "completed",
                     output: list[dict], input_text: str, output_text: str,
                     instructions: str | None, previous_response_id: str | None,
                     store: bool, tools: list[dict] | None,
                     max_output_tokens: int | None, text_format: dict,
                     incomplete: dict | None = None) -> dict:
    return {"id": resp_id, "object": "response", "created_at": created, "model": model,
            "status": status, "error": None, "incomplete_details": incomplete,
            "instructions": instructions, "metadata": {},
            "output": output, "parallel_tool_calls": True,
            "tool_choice": "auto", "tools": tools or [],
            "temperature": None, "top_p": None,
            "max_output_tokens": max_output_tokens,
            "previous_response_id": previous_response_id,
            "reasoning": {"effort": None, "summary": None},
            "store": store, "text": {"format": text_format},
            "truncation": "disabled",
            "usage": _usage(input_text, output_text), "user": None}


@router.post("/v1/responses")
async def create_response(body: ResponseRequest, request: Request,
                          svc: Services = Depends(get_services)):
    spec = resolve_model(body.model, "chat")
    request.state.model = spec.id  # log the actual model, not the client alias

    own_messages = _to_messages(body)
    chain_hint = None
    if body.previous_response_id:
        entry = svc.responses.get(body.previous_response_id)
        if entry is None:
            raise NotFound(
                f"No response found with id '{body.previous_response_id}'.",
                param="previous_response_id")
        stored = entry.get("messages") or []
        # Fresh instructions replace the stored system turn (OpenAI semantics).
        if body.instructions:
            stored = [m for m in stored if m.get("role") not in ("system", "developer")]
        own_messages = stored + own_messages
        chain_hint = entry.get("hint")

    if not own_messages:
        raise InvalidRequest("input contains no text or images", param="input")

    # Responses fields mapped onto the chat planner: tools/tool_choice, and
    # text.format → response_format; max_output_tokens → max_tokens (the cap).
    response_format, text_format = _text_format_to_response_format(body.text)
    chat_body = ChatCompletionRequest(
        model=body.model,
        messages=[ChatMessage(role=m["role"], content=m.get("content")) for m in own_messages],
        tools=body.tools or None,
        tool_choice=body.tool_choice,
        max_tokens=body.max_output_tokens,
        response_format=response_format,
    )
    plan = _build_plan(chat_body, own_messages, svc.settings)
    if not plan.prompt and not plan.images:
        raise InvalidRequest("input contains no text or images", param="input")
    if plan.json_directive:
        plan.prompt = f"{plan.json_directive}\n\n{plan.prompt}"
        if plan.followup_prompt:
            plan.followup_prompt = f"{plan.json_directive}\n\n{plan.followup_prompt}"

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

    def save_chain(final_message: dict) -> None:
        """Record this response's chat messages so later calls can continue it."""
        if body.store:
            svc.responses.save(resp_id, model=model_name,
                               messages=own_messages + [final_message], hint=hint)

    if not body.stream:
        text = await _collect(stream)
        text = await _retry_nonstream(text, plan, req, svc, own_messages)
        message, finish = _message_from_text(plan, text)
        if message.get("tool_calls"):
            output = [_function_call_item(call) for call in message["tool_calls"]]
            usage_text = json.dumps(message["tool_calls"], ensure_ascii=False)
        else:
            content = message.get("content") or ""
            output = [_message_item(msg_id, content)]
            usage_text = content
        incomplete = {"reason": "max_output_tokens"} if finish == "length" else None
        save_chain(message)
        return _response_object(
            resp_id, created, model_name,
            status="incomplete" if incomplete else "completed",
            output=output, input_text=input_text, output_text=usage_text,
            instructions=body.instructions,
            previous_response_id=body.previous_response_id,
            store=body.store, tools=body.tools,
            max_output_tokens=body.max_output_tokens,
            text_format=text_format, incomplete=incomplete)

    # --- streaming -----------------------------------------------------------

    if buffered:
        # Planner JSON must be rewritten into function_call items (or JSON mode
        # extracted) before any event goes out, so buffer fully, fail fast,
        # then replay with the standard event sequence.
        text = await _collect(stream)
        text = await _retry_nonstream(text, plan, req, svc, own_messages)
        message, _finish = _message_from_text(plan, text)

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
                                text_format=text_format)})
                seq += 1
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
                    content = message.get("content") or ""
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
                                         "annotations": []}})
                    seq += 1
                    yield _sse({"type": "response.output_text.delta",
                                "sequence_number": seq, "item_id": msg_id,
                                "output_index": 0, "content_index": 0,
                                "delta": content, "logprobs": []})
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
                                         "annotations": []}})
                    seq += 1
                    yield _sse({"type": "response.output_item.done",
                                "sequence_number": seq, "output_index": 0,
                                "item": _message_item(msg_id, content)})
                    seq += 1
                completed = _response_object(
                    resp_id, created, model_name, output=items if message.get("tool_calls")
                    else [_message_item(msg_id, message.get("content") or "")],
                    input_text=input_text,
                    output_text=message.get("content") or
                    json.dumps(message.get("tool_calls") or [], ensure_ascii=False),
                    instructions=body.instructions,
                    previous_response_id=body.previous_response_id,
                    store=body.store, tools=body.tools,
                    max_output_tokens=body.max_output_tokens,
                    text_format=text_format)
                yield _sse({"type": "response.completed", "sequence_number": seq,
                            "response": completed})
                save_chain(message)
            except Muse2APIError as exc:
                yield _sse(exc.to_body())
            except Exception:  # noqa: BLE001
                log.exception("responses stream crashed")
                yield _sse({"error": {"message": "internal error",
                                      "type": "server_error",
                                      "param": None, "code": "internal_error"}})
            finally:
                cancel.set()
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
                            text_format=text_format)})
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
                            text_format=text_format)})
            seq += 1
            yield _sse({"type": "response.output_item.added", "sequence_number": seq,
                        "output_index": 0,
                        "item": {"id": msg_id, "type": "message", "status": "in_progress",
                                 "role": "assistant", "content": []}})
            seq += 1
            yield _sse({"type": "response.content_part.added", "sequence_number": seq,
                        "item_id": msg_id, "output_index": 0, "content_index": 0,
                        "part": {"type": "output_text", "text": "", "annotations": []}})
            seq += 1
            produced = first or ""

            def delta_event(delta: str) -> dict:
                return {"type": "response.output_text.delta", "sequence_number": seq,
                        "item_id": msg_id, "output_index": 0, "content_index": 0,
                        "delta": delta, "logprobs": []}

            hit_cap = False
            if plan.max_chars is not None and len(produced) > plan.max_chars:
                produced = produced[:plan.max_chars]
                hit_cap = True
            elif produced:
                yield _sse(delta_event(produced))
                seq += 1
                async for delta in stream:
                    produced += delta
                    if plan.max_chars is not None and len(produced) > plan.max_chars:
                        # OpenAI marks the response incomplete; the tail beyond
                        # the cap is dropped and the upstream turn cancelled.
                        produced = produced[:plan.max_chars]
                        hit_cap = True
                        break
                    yield _sse(delta_event(delta))
                    seq += 1
            if hit_cap:
                cancel.set()
            yield _sse({"type": "response.output_text.done", "sequence_number": seq,
                        "item_id": msg_id, "output_index": 0, "content_index": 0,
                        "text": produced, "logprobs": []})
            seq += 1
            yield _sse({"type": "response.content_part.done", "sequence_number": seq,
                        "item_id": msg_id, "output_index": 0, "content_index": 0,
                        "part": {"type": "output_text", "text": produced,
                                 "annotations": []}})
            seq += 1
            yield _sse({"type": "response.output_item.done", "sequence_number": seq,
                        "output_index": 0, "item": _message_item(msg_id, produced)})
            seq += 1
            incomplete = {"reason": "max_output_tokens"} if hit_cap else None
            final = _response_object(
                resp_id, created, model_name,
                status="incomplete" if incomplete else "completed",
                output=[_message_item(msg_id, produced)],
                input_text=input_text, output_text=produced,
                instructions=body.instructions,
                previous_response_id=body.previous_response_id,
                store=body.store, tools=body.tools,
                max_output_tokens=body.max_output_tokens,
                text_format=text_format, incomplete=incomplete)
            yield _sse({"type": "response.completed" if not incomplete
                        else "response.incomplete",
                        "sequence_number": seq, "response": final})
            save_chain({"role": "assistant", "content": produced})
        except Muse2APIError as exc:
            yield _sse(exc.to_body())
        except Exception:  # noqa: BLE001
            log.exception("responses stream crashed")
            yield _sse({"error": {"message": "internal error", "type": "server_error",
                                  "param": None, "code": "internal_error"}})
        finally:
            cancel.set()
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
