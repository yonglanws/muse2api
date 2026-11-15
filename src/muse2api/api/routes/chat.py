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

from ...core import astrbot
from ...core.media import load_image_ref
from ...core.models import resolve_model
from ...core.prompt import flatten_messages, message_turns
from ...drivers.base import ChatRequest, InputImage
from ...errors import InvalidRequest, Muse2APIError
from ...services.container import Services
from ..deps import astrbot_conversation_id, conversation_key, get_services, require_api_key
from ..schemas import ChatCompletionRequest

log = logging.getLogger(__name__)
router = APIRouter(tags=["chat"], dependencies=[Depends(require_api_key)])


def _approx_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _sse(payload: dict | str) -> bytes:
    data = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return f"data: {data}\n\n".encode()


def _aliases(settings) -> list[str]:
    return [part.strip() for part in settings.astrbot_aliases.split(",") if part.strip()]


def _astrbot_mode(settings, request: Request, messages: list[dict]) -> bool:
    if settings.astrbot_compat == "on":
        return True
    if settings.astrbot_compat == "off":
        return False
    return astrbot.detect(request.headers, messages)


@dataclass
class _Plan:
    prompt: str
    followup_prompt: str | None
    turns: list[tuple[str, str]]
    images: list[str]
    images_current_only: bool
    system_prompt: str
    current_user_text: str
    user_nickname: str | None
    identity_question: bool
    has_persona: bool
    persona_fp: str | None
    astrbot: bool
    tools: list[dict] | None
    conversation_id: str | None = None
    force_tool: bool = False


def _select_tools(tools: list[dict] | None, tool_choice) -> tuple[list[dict] | None, bool]:
    """Apply OpenAI ``tool_choice`` semantics to the emulated tool list."""
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


def _build_plan(body: ChatCompletionRequest, request: Request, messages: list[dict],
                settings) -> _Plan:
    use_astrbot = _astrbot_mode(settings, request, messages)
    tools, force_tool = _select_tools(body.tools, body.tool_choice)
    if not settings.emulate_tools:
        tools, force_tool = None, False
    if use_astrbot:
        if tools:
            # A flat JSON planner is far more reliable than asking muse.ai to
            # emit nested OpenAI tool_calls, which it treats as fake capability.
            ab = astrbot.build_tool_prompt(
                messages,
                tools,
                conversation_id=astrbot_conversation_id(request),
                max_prompt_chars=settings.astrbot_max_prompt_chars,
                force_tool=force_tool,
            )
        else:
            ab = astrbot.build_prompt(
                messages,
                conversation_id=astrbot_conversation_id(request),
                max_prompt_chars=settings.astrbot_max_prompt_chars,
                force_tool=force_tool,
            )
        persona_fp = (astrbot.persona_fingerprint(ab.system_prompt)
                      if ab.system_prompt else None)
        if persona_fp and settings.astrbot_persona_sticky:
            persona_fp = "sticky"
        return _Plan(
            prompt=ab.prompt,
            followup_prompt=ab.followup_prompt,
            turns=ab.turns,
            images=ab.images,
            images_current_only=True,
            system_prompt=ab.system_prompt,
            current_user_text=ab.current_user_text,
            user_nickname=ab.user_nickname,
            identity_question=ab.identity_question,
            has_persona=ab.has_persona,
            persona_fp=persona_fp,
            astrbot=True,
            tools=tools,
            conversation_id=astrbot_conversation_id(request),
            force_tool=force_tool,
        )

    flat = flatten_messages(messages)
    prompt = flat.text
    if tools:
        protocol = astrbot.tool_protocol_text(tools, force_tool=force_tool)
        prompt = f"{prompt}\n\n{protocol}".strip()
    system_prompt = "\n\n".join(
        text for role, text in message_turns(messages)
        if role in ("system", "developer") and text.strip()
    ).strip()
    if system_prompt:
        # The same persona bridge AstrBot uses is the only framing that reliably
        # wins over Muse's built-in identity. Apply it to ordinary OpenAI clients
        # too, but leave requests without a system prompt on the old path.
        if tools:
            ab = astrbot.build_tool_prompt(
                messages, tools,
                max_prompt_chars=settings.astrbot_max_prompt_chars,
                force_tool=force_tool,
            )
        else:
            ab = astrbot.build_prompt(
                messages, max_prompt_chars=settings.astrbot_max_prompt_chars,
                force_tool=force_tool,
            )
        persona_fp = (astrbot.persona_fingerprint(ab.system_prompt)
                      if ab.system_prompt else None)
        return _Plan(
            prompt=ab.prompt,
            followup_prompt=ab.followup_prompt,
            turns=ab.turns,
            images=ab.images,
            images_current_only=True,
            system_prompt=ab.system_prompt,
            current_user_text=ab.current_user_text,
            user_nickname=ab.user_nickname,
            identity_question=ab.identity_question,
            has_persona=ab.has_persona,
            persona_fp=persona_fp,
            astrbot=True,
            tools=tools,
            force_tool=force_tool,
        )
    turns = message_turns(messages)
    current = next((text for role, text in reversed(turns) if role == "user"), "")
    return _Plan(
        prompt=prompt,
        followup_prompt=None,
        turns=turns,
        images=flat.images,
        images_current_only=False,
        system_prompt=system_prompt,
        current_user_text=current,
        user_nickname=None,
        identity_question=astrbot.is_identity_question(current),
        has_persona=False,
        persona_fp=None,
        astrbot=False,
        tools=tools,
        force_tool=force_tool,
    )


def _clean_text(text: str, plan: _Plan, settings) -> str:
    cleaned = astrbot.strip_text_prefix(text or "").strip()
    if plan.astrbot and settings.astrbot_sanitize:
        cleaned = astrbot.sanitize_reply(
            cleaned,
            has_persona=plan.has_persona,
            persona=plan.system_prompt,
            user_nickname=plan.user_nickname,
            aliases=_aliases(settings),
            identity_question=plan.identity_question,
        )
    if not cleaned and plan.astrbot:
        return "我在的～"
    if (plan.astrbot and plan.has_persona and cleaned == "我在的～"
            and (text or "").strip() and not plan.identity_question):
        log.info("AstrBot reply needed the generic fallback; raw=%r", (text or "")[:400])
    return cleaned


def _is_generic_fallback(text: str, plan: _Plan, settings) -> bool:
    """The sanitizer had to throw the whole raw reply away."""
    return (plan.astrbot and plan.has_persona and not plan.tools
            and not plan.identity_question
            and _clean_text(text, plan, settings) == "我在的～")


def _usage(prompt_text: str, completion_text: str) -> dict:
    p, c = _approx_tokens(prompt_text), _approx_tokens(completion_text)
    return {"prompt_tokens": p, "completion_tokens": c, "total_tokens": p + c}


def _needs_tool_retry(text: str, plan: _Plan) -> bool:
    if not plan.tools:
        return False
    tool, _, reply = astrbot.parse_tool_plan(text, plan.tools)
    if tool or reply is not None:
        return False
    if plan.force_tool:
        return True
    # Malformed planner JSON (unknown tool / broken arguments) is worth one
    # fresh attempt; a normal prose answer that merely mentions "tool" is not.
    if re.search(r'"(?:tool|tool_calls)"\s*:', text or ""):
        return True
    return astrbot.looks_like_tool_refusal(text) or astrbot.looks_like_persona_refusal(text)


def _needs_persona_retry(text: str, plan: _Plan) -> bool:
    return (plan.astrbot and plan.has_persona and not plan.tools
            and astrbot.looks_like_persona_refusal(text))


def _retry_request(plan: _Plan, req: ChatRequest, svc: Services, messages: list[dict],
                   kind: str) -> ChatRequest:
    """A fresh-thread retry request. Persona is removed for tool retries so the
    model can focus on the JSON protocol; a refused persona falls back to style."""
    style = astrbot.style_persona(plan.system_prompt) if plan.has_persona else None
    if kind == "tool":
        prompt = astrbot.build_tool_retry_prompt(
            messages, plan.tools, conversation_id=plan.conversation_id,
            style_hint=style,
        )
    else:
        prompt = astrbot.build_persona_retry_prompt(
            messages, style_prompt=style or "说话自然、简短，用第一人称。",
            conversation_id=plan.conversation_id,
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
        persona_fingerprint=plan.persona_fp,
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
    """Consume a text stream, yielding SSE keepalives while it is idle.

    The complete text is appended to ``holder``; a generator cannot return it.
    """
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
        for _ in range(max(0, svc.settings.astrbot_tool_retries)):
            attempt = _retry_request(plan, req, svc, messages, "tool")
            text = await _collect(svc.gateway.chat_stream(attempt))
            if not _needs_tool_retry(text, plan):
                return text
        return text
    if _needs_persona_retry(text, plan) or _is_generic_fallback(text, plan, svc.settings):
        for _ in range(max(0, svc.settings.astrbot_persona_retries)):
            attempt = _retry_request(plan, req, svc, messages, "persona")
            text = await _collect(svc.gateway.chat_stream(attempt))
            if (not _needs_persona_retry(text, plan)
                    and not _is_generic_fallback(text, plan, svc.settings)):
                return text
    return text


@router.post("/v1/chat/completions")
async def chat_completions(body: ChatCompletionRequest, request: Request,
                           svc: Services = Depends(get_services)):
    request.state.model = body.model  # logged even if it does not resolve
    spec = resolve_model(body.model, "chat")
    request.state.model = body.model or spec.id
    messages = [m.model_dump() for m in body.messages]
    plan = _build_plan(body, request, messages, svc.settings)
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
        tool_continuation=plan.astrbot and astrbot.is_tool_continuation(messages),
        images_current_only=plan.images_current_only,
        persona_fingerprint=plan.persona_fp,
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
        return _completion(plan, text, body, svc, completion_id, created, model_name)

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
        """Buffer the whole reply so tool calls and disclaimers can be handled."""
        produced = ""
        interval = svc.settings.stream_heartbeat_interval
        try:
            holder: list[str] = []
            initial = "" if first is None else (first or "")
            async for piece in _pump_stream(
                    stream, interval if first is None else 0, holder, initial=initial):
                yield piece
            produced = holder[-1] if holder else ""
            # A refused tool call/persona gets a fresh thread and a stricter
            # prompt. This is deliberately buffered: the client sees one answer,
            # never the refusal that triggered the retry.
            if _needs_tool_retry(produced, plan):
                for _ in range(max(0, svc.settings.astrbot_tool_retries)):
                    attempt = _retry_request(plan, req, svc, messages, "tool")
                    holder = []
                    async for piece in _pump_stream(
                            svc.gateway.chat_stream(attempt), interval, holder):
                        yield piece
                    produced = holder[-1] if holder else ""
                    if not _needs_tool_retry(produced, plan):
                        break
            elif _needs_persona_retry(produced, plan) or _is_generic_fallback(
                    produced, plan, svc.settings):
                for _ in range(max(0, svc.settings.astrbot_persona_retries)):
                    attempt = _retry_request(plan, req, svc, messages, "persona")
                    holder = []
                    async for piece in _pump_stream(
                            svc.gateway.chat_stream(attempt), interval, holder):
                        yield piece
                    produced = holder[-1] if holder else ""
                    if (not _needs_persona_retry(produced, plan)
                            and not _is_generic_fallback(produced, plan, svc.settings)):
                        break
            tool = None
            reply = None
            if plan.tools:
                tool, args, reply = astrbot.parse_tool_plan(produced, plan.tools)
            if tool:
                call = astrbot.make_tool_call(tool, args or {})
                yield _sse(chunk({"role": "assistant", "content": None, "tool_calls": [
                    {"index": 0, "id": call["id"], "type": "function",
                     "function": {"name": call["function"]["name"], "arguments": ""}}
                ]}))
                yield _sse(chunk({"tool_calls": [
                    {"index": 0, "function": {"arguments": call["function"]["arguments"]}}
                ]}))
                yield _sse(chunk({}, "tool_calls"))
            else:
                cleaned = _clean_text(reply if reply is not None else produced,
                                      plan, svc.settings)
                yield _sse(chunk({"role": "assistant", "content": ""}))
                # A buffered reply arrives as a few chunks, not one giant frame.
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
        # ``first`` is the pre-fetched first delta when heartbeats are disabled;
        # when enabled it is None and the first token is awaited here, after the
        # 200 (headers) has already been committed to the client.
        try:
            if first is None and svc.settings.stream_heartbeat_interval > 0:
                # C4: some clients/proxies drop idle SSE connections while the
                # gateway waits for the first upstream token (up to
                # first_token_timeout). Send SSE comment heartbeats until it
                # arrives. asyncio.wait (not wait_for) polls so the in-flight
                # __anext__ is never cancelled between heartbeats.
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

    buffered = bool(plan.tools) or (plan.astrbot and svc.settings.astrbot_buffer_stream)
    if svc.settings.stream_heartbeat_interval > 0:
        return StreamingResponse(buffered_events(None) if buffered else events(None),
                                 media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    # Default: pull the first delta before committing to a 200 so that
    # account/upstream failures surface as proper HTTP errors instead of a
    # broken event stream.
    first = await first_delta()
    return StreamingResponse(buffered_events(first) if buffered else events(first),
                             media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _completion(plan: _Plan, text: str, body: ChatCompletionRequest, svc: Services,
                completion_id: str, created: int, model_name: str) -> dict:
    message: dict = {}
    finish = "stop"
    if plan.tools:
        tool, args, reply = astrbot.parse_tool_plan(text, plan.tools)
        if tool:
            message = {"role": "assistant", "content": None,
                       "tool_calls": [astrbot.make_tool_call(tool, args or {})]}
            finish = "tool_calls"
        elif reply is not None:
            message = {"role": "assistant", "content": _clean_text(reply, plan, svc.settings)}
        else:
            message = {"role": "assistant", "content": _clean_text(text, plan, svc.settings)}
    else:
        message = {"role": "assistant", "content": _clean_text(text, plan, svc.settings)}
    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": model_name,
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": _usage(plan.prompt, text),
    }
