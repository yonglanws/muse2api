from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from ...core import system_prompt, tool_plan
from ...core.media import load_image_ref
from ...core.models import resolve_model
from ...core.prompt import flatten_messages, message_turns
from ...drivers.base import ChatRequest, InputImage
from ...errors import InvalidRequest, Muse2APIError
from ...services.container import Services
from ..deps import conversation_key, get_services, require_api_key
from ..schemas import ChatCompletionRequest

log = logging.getLogger(__name__)
router = APIRouter(tags=["chat"], dependencies=[Depends(require_api_key)])


def _approx_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _sse(payload: dict | str) -> bytes:
    data = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return f"data: {data}\n\n".encode()


@dataclass
class _Plan:
    prompt: str
    followup_prompt: str | None
    turns: list[tuple[str, str]]
    images: list[str]
    images_current_only: bool
    system_prompt: str
    current_user_text: str
    system_fp: str | None
    tools: list[dict] | None
    force_tool: bool = False
    tool_continuation: bool = False


def _select_tools(tools: list[dict] | None, tool_choice) -> tuple[list[dict] | None, bool]:
    """Apply OpenAI ``tool_choice`` semantics to the tool list."""
    if not tools:
        return None, False
    if tool_choice == "none":
        return None, False
    if isinstance(tool_choice, dict):
        fn = tool_choice.get("function") if isinstance(tool_choice.get("function"), dict) else {}
        name = fn.get("name") or tool_choice.get("name")
        if name:
            picked = [t for t in tools
                      if str((t.get("function") or {}).get("name") or "") == str(name)]
            return (picked or tools), True
    return tools, tool_choice == "required"


def _build_plan(body: ChatCompletionRequest, messages: list[dict], settings) -> _Plan:
    tools, force_tool = _select_tools(body.tools, body.tool_choice)
    if not settings.emulate_tools:
        tools, force_tool = None, False

    if tools:
        plan = system_prompt.build_tool_prompt(
            messages,
            tools,
            max_prompt_chars=settings.max_prompt_chars,
            force_tool=force_tool,
            native=settings.tool_channel != "planner",
        )
        return _Plan(
            prompt=plan.prompt,
            followup_prompt=plan.followup_prompt,
            turns=plan.turns,
            images=plan.images,
            images_current_only=True,
            system_prompt=plan.system_prompt,
            current_user_text=plan.current_user_text,
            system_fp=plan.system_fingerprint,
            tools=tools,
            force_tool=force_tool,
            tool_continuation=plan.tool_continuation,
        )

    system = system_prompt.extract_system_prompt(messages)
    if system:
        plan = system_prompt.build_prompt(messages, max_prompt_chars=settings.max_prompt_chars)
        return _Plan(
            prompt=plan.prompt,
            followup_prompt=plan.followup_prompt,
            turns=plan.turns,
            images=plan.images,
            images_current_only=True,
            system_prompt=plan.system_prompt,
            current_user_text=plan.current_user_text,
            system_fp=plan.system_fingerprint,
            tools=None,
            tool_continuation=plan.tool_continuation,
        )

    flat = flatten_messages(messages)
    turns = message_turns(messages)
    current = next((text for role, text in reversed(turns) if role == "user"), "")
    return _Plan(
        prompt=flat.text,
        followup_prompt=None,
        turns=turns,
        images=flat.images,
        images_current_only=False,
        system_prompt="",
        current_user_text=current,
        system_fp=None,
        tools=None,
    )


def _clean_text(text: str) -> str:
    return tool_plan.strip_text_prefix(text or "").strip()


def _usage(prompt_text: str, completion_text: str) -> dict:
    p, c = _approx_tokens(prompt_text), _approx_tokens(completion_text)
    return {"prompt_tokens": p, "completion_tokens": c, "total_tokens": p + c}


def _needs_tool_retry(text: str, plan: _Plan) -> bool:
    if not plan.tools:
        return False
    if tool_plan.parse_tool_calls_list(text, plan.tools) is not None:
        return False
    tool, _, reply = tool_plan.parse_tool_plan(text, plan.tools)
    if tool or reply is not None:
        return False
    if plan.force_tool:
        return True
    if re.search(r'"(?:tool|tool_calls)"\s*:', text or ""):
        return True
    return tool_plan.looks_like_tool_refusal(text)


def _retry_request(plan: _Plan, req: ChatRequest, messages: list[dict],
                  max_prompt_chars: int = 18000) -> ChatRequest:
    """A fresh-thread retry with a stricter planner prompt."""
    prompt = system_prompt.build_tool_retry_prompt(
        messages, plan.tools or [], max_prompt_chars=max_prompt_chars,
    )
    return ChatRequest(
        prompt=prompt,
        model=req.model,
        images=req.images,
        timeout=req.timeout,
        first_token_timeout=req.first_token_timeout,
        conversation_hint=req.conversation_hint,
        turns=plan.turns,
        followup_prompt=None,
        tool_continuation=False,
        images_current_only=plan.images_current_only,
        system_fingerprint=plan.system_fp,
        page_echo=req.page_echo,
        force_new_thread=True,
        tools=plan.tools,
        system_prompt=plan.system_prompt or None,
        cancel=req.cancel,
    )


async def _collect(stream) -> str:
    return "".join([delta async for delta in stream])


async def _pump_stream(stream, interval: float, holder: list[str],
                       initial: str = "") -> AsyncIterator[bytes]:
    """Consume a text stream, yielding SSE keepalives while it is idle."""
    parts = [initial] if initial else []
    try:
        if interval > 0:
            pending = asyncio.ensure_future(anext(stream))
            try:
                while True:
                    done, _ = await asyncio.wait({pending}, timeout=interval)
                    if not done:
                        yield b": keepalive\n\n"
                        continue
                    try:
                        parts.append(pending.result())
                    except StopAsyncIteration:
                        break
                    pending = asyncio.ensure_future(anext(stream))
            finally:
                if not pending.done():
                    pending.cancel()
        else:
            async for delta in stream:
                parts.append(delta)
    finally:
        with contextlib.suppress(Exception):
            await stream.aclose()
    holder.append("".join(parts))


async def _retry_nonstream(text: str, plan: _Plan, req: ChatRequest, svc: Services,
                           messages: list[dict]) -> str:
    if _needs_tool_retry(text, plan):
        for _ in range(max(0, svc.settings.tool_retries)):
            attempt = _retry_request(plan, req, messages, svc.settings.max_prompt_chars)
            text = await _collect(svc.gateway.chat_stream(attempt))
            if not _needs_tool_retry(text, plan):
                return text
    return text


@router.post("/v1/chat/completions")
async def chat_completions(body: ChatCompletionRequest, request: Request,
                           svc: Services = Depends(get_services)):
    request.state.model = body.model  # logged even if it does not resolve
    spec = resolve_model(body.model, "chat")
    request.state.model = body.model or spec.id
    messages = [m.model_dump() for m in body.messages]
    plan = _build_plan(body, messages, svc.settings)
    if not plan.prompt and not plan.images:
        raise InvalidRequest("messages contain no text or images")

    images = [InputImage(*(await load_image_ref(ref))) for ref in plan.images]
    cancel = asyncio.Event()
    req = ChatRequest(
        prompt=plan.prompt,
        model=spec.id,
        images=images,
        timeout=svc.settings.chat_timeout,
        first_token_timeout=svc.settings.first_token_timeout,
        conversation_hint=conversation_key(request, body.user),
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
    completion_id = "chatcmpl-" + uuid.uuid4().hex[:24]
    created = int(time.time())
    model_name = body.model or spec.id
    stream = svc.gateway.chat_stream(req)

    if not body.stream:
        text = await _collect(stream)
        text = await _retry_nonstream(text, plan, req, svc, messages)
        return _completion(plan, text, completion_id, created, model_name)

    def chunk(delta: dict, finish: str | None = None) -> dict:
        return {"id": completion_id, "object": "chat.completion.chunk", "created": created,
                "model": model_name,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}

    async def first_delta() -> str | None:
        """Prefetch the first upstream delta so failures become HTTP errors."""
        try:
            return await anext(stream)
        except StopAsyncIteration:
            return ""

    async def buffered_events(first: str | None) -> AsyncIterator[bytes]:
        """Buffer a tool-enabled reply so planner JSON can be rewritten."""
        produced = ""
        interval = svc.settings.stream_heartbeat_interval
        try:
            holder: list[str] = []
            initial = "" if first is None else (first or "")
            async for piece in _pump_stream(
                    stream, interval if first is None else 0, holder, initial=initial):
                yield piece
            produced = holder[-1] if holder else ""
            if _needs_tool_retry(produced, plan):
                for _ in range(max(0, svc.settings.tool_retries)):
                    attempt = _retry_request(plan, req, messages, svc.settings.max_prompt_chars)
                    holder = []
                    async for piece in _pump_stream(
                            svc.gateway.chat_stream(attempt), interval, holder):
                        yield piece
                    produced = holder[-1] if holder else ""
                    if not _needs_tool_retry(produced, plan):
                        break
            tool = None
            reply = None
            calls = tool_plan.parse_tool_calls_list(produced, plan.tools) if plan.tools else None
            if calls is None and plan.tools:
                tool, args, reply = tool_plan.parse_tool_plan(produced, plan.tools)
            if calls:
                encoded = [tool_plan.make_tool_call(name, args or {})
                           for name, args in calls]
                yield _sse(chunk({"role": "assistant", "content": None, "tool_calls": [
                    {"index": i, "id": call["id"], "type": "function",
                     "function": {"name": call["function"]["name"], "arguments": ""}}
                    for i, call in enumerate(encoded)
                ]}))
                for i, call in enumerate(encoded):
                    yield _sse(chunk({"tool_calls": [
                        {"index": i, "function": {"arguments": call["function"]["arguments"]}}
                    ]}))
                yield _sse(chunk({}, "tool_calls"))
            elif tool:
                call = tool_plan.make_tool_call(tool, args or {})
                yield _sse(chunk({"role": "assistant", "content": None, "tool_calls": [
                    {"index": 0, "id": call["id"], "type": "function",
                     "function": {"name": call["function"]["name"], "arguments": ""}}
                ]}))
                yield _sse(chunk({"tool_calls": [
                    {"index": 0, "function": {"arguments": call["function"]["arguments"]}}
                ]}))
                yield _sse(chunk({}, "tool_calls"))
            else:
                cleaned = _clean_text(reply if reply is not None else produced)
                yield _sse(chunk({"role": "assistant", "content": ""}))
                for i in range(0, len(cleaned), 24):
                    yield _sse(chunk({"content": cleaned[i:i + 24]}))
                yield _sse(chunk({}, "stop"))
            if (body.stream_options or {}).get("include_usage"):
                usage = _usage(plan.prompt, produced)
                yield _sse({"id": completion_id, "object": "chat.completion.chunk",
                            "created": created, "model": model_name, "choices": [],
                            "usage": usage})
        except Muse2APIError as exc:
            yield _sse(exc.to_body())
        except Exception:  # noqa: BLE001
            log.exception("stream crashed")
            yield _sse({"error": {"message": "internal error", "type": "server_error"}})
        finally:
            cancel.set()
            await stream.aclose()
        yield _sse("[DONE]")

    async def events(first: str | None) -> AsyncIterator[bytes]:
        try:
            if first is None and svc.settings.stream_heartbeat_interval > 0:
                pending = asyncio.ensure_future(anext(stream))
                try:
                    while True:
                        done, _ = await asyncio.wait(
                            {pending}, timeout=svc.settings.stream_heartbeat_interval)
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
            produced = first or ""
            yield _sse(chunk({"role": "assistant", "content": produced}))
            async for delta in stream:
                produced += delta
                yield _sse(chunk({"content": delta}))
            yield _sse(chunk({}, "stop"))
            if (body.stream_options or {}).get("include_usage"):
                usage = _usage(plan.prompt, produced)
                yield _sse({"id": completion_id, "object": "chat.completion.chunk",
                            "created": created, "model": model_name, "choices": [],
                            "usage": usage})
        except Muse2APIError as exc:
            yield _sse(exc.to_body())
        except Exception:  # noqa: BLE001
            log.exception("stream crashed")
            yield _sse({"error": {"message": "internal error", "type": "server_error"}})
        finally:
            cancel.set()
            await stream.aclose()
        yield _sse("[DONE]")

    buffered = bool(plan.tools)
    if svc.settings.stream_heartbeat_interval > 0:
        return StreamingResponse(buffered_events(None) if buffered else events(None),
                                 media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    first = await first_delta()
    return StreamingResponse(buffered_events(first) if buffered else events(first),
                             media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _completion(plan: _Plan, text: str,
                completion_id: str, created: int, model_name: str) -> dict:
    message: dict = {}
    finish = "stop"
    if plan.tools:
        calls = tool_plan.parse_tool_calls_list(text, plan.tools)
        if calls:
            message = {"role": "assistant", "content": None,
                       "tool_calls": [tool_plan.make_tool_call(name, args or {})
                                      for name, args in calls]}
            finish = "tool_calls"
        else:
            tool, args, reply = tool_plan.parse_tool_plan(text, plan.tools)
            if tool:
                message = {"role": "assistant", "content": None,
                           "tool_calls": [tool_plan.make_tool_call(tool, args or {})]}
                finish = "tool_calls"
            elif reply is not None:
                message = {"role": "assistant", "content": _clean_text(reply)}
            else:
                message = {"role": "assistant", "content": _clean_text(text)}
    else:
        message = {"role": "assistant", "content": _clean_text(text)}
    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": model_name,
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": _usage(plan.prompt, text),
    }
