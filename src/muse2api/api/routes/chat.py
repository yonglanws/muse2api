from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace

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
    stop: list[str] | None = None
    json_directive: str | None = None
    # max_tokens mapped to an approximate character cap (muse.ai exposes no
    # sampling control; truncation is the only honest way to honor the limit).
    max_chars: int | None = None


_JSON_DIRECTIVE = (
    "[[OUTPUT FORMAT]]\n"
    "Respond with a single valid JSON value and nothing else: no prose before or "
    "after it and no markdown code fences. Your entire reply must be accepted by "
    "JSON.parse."
)
_MAX_SCHEMA_CHARS = 6000


def _json_directive(response_format) -> str | None:
    """OpenAI ``response_format`` rendered as a prompt directive (muse.ai has
    no native JSON mode; probe 16 shows the sampling surface is fixed)."""
    if not isinstance(response_format, dict):
        return None
    rtype = response_format.get("type")
    if rtype == "json_object":
        return _JSON_DIRECTIVE
    if rtype == "json_schema":
        schema = response_format.get("json_schema") or {}
        if not isinstance(schema, dict):
            return _JSON_DIRECTIVE
        name = schema.get("name") or "response"
        body = json.dumps(schema.get("schema") or {}, ensure_ascii=False)[:_MAX_SCHEMA_CHARS]
        return (_JSON_DIRECTIVE
                + f"\nThe JSON must conform to this JSON Schema ('{name}'):\n{body}")
    return None


def _stop_list(stop) -> list[str] | None:
    if isinstance(stop, str) and stop:
        return [stop]
    if isinstance(stop, list):
        seqs = [s for s in stop if isinstance(s, str) and s]
        return seqs or None
    return None


def _cap_from_max_tokens(max_tokens: int | None) -> int | None:
    """``max_tokens`` (a completion-token cap) → an approximate character cap.

    _approx_tokens counts len//4, so limit*4 characters keeps the reported
    token count within the budget. Applies to plain-text replies only: cutting
    a tool-call JSON mid-argument would corrupt it.
    """
    if max_tokens and max_tokens > 0:
        return max_tokens * 4
    return None


def _first_stop_index(text: str, stop: list[str] | None) -> int | None:
    if not stop or not text:
        return None
    best = None
    for seq in stop:
        i = text.find(seq)
        if i != -1 and (best is None or i < best):
            best = i
    return best


def _apply_stop(text: str, stop: list[str] | None) -> str:
    i = _first_stop_index(text, stop)
    return text if i is None else text[:i]


def _extract_json(text: str) -> str | None:
    """Best-effort extraction of the JSON value from a model reply."""
    s = (text or "").strip()
    fence = re.search(r"```(?:json|[A-Za-z0-9_-]*)[ \t]*\r?\n(.*?)\r?\n?[ \t]*```",
                      s, re.DOTALL)
    if fence:
        s = fence.group(1).strip()
    elif s.startswith("```"):
        s = re.sub(r"^```[A-Za-z0-9_-]*[ \t]*\r?\n?", "", s)
        s = re.sub(r"\r?\n?[ \t]*```\s*$", "", s).strip()
    if s.startswith(("{", "[")):
        closer = "}" if s[0] == "{" else "]"
        end = s.rfind(closer)
        if end > 0:
            return s[:end + 1]
    # prose around a bare JSON value: only accept a parseable candidate
    for opener, closer in (("{", "}"), ("[", "]")):
        start = s.find(opener)
        if start != -1:
            end = s.rfind(closer)
            if end > start:
                candidate = s[start:end + 1]
                try:
                    json.loads(candidate)
                except ValueError:
                    continue
                return candidate
    if s.startswith('"') and s.endswith('"') and len(s) >= 2:
        try:
            json.loads(s)
        except ValueError:
            return None
        return s
    return None


def _needs_json_retry(text: str) -> bool:
    value = _extract_json(text)
    if value is None:
        return True
    try:
        json.loads(value)
    except ValueError:
        return True
    return False


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
    # JSON mode rides on a prompt directive; with tools the reply is a tool
    # call anyway, so the directive would only corrupt it.
    json_directive = None if tools else _json_directive(body.response_format)
    stop = _stop_list(body.stop)
    cap = _cap_from_max_tokens(body.max_tokens)

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
            stop=stop,
            max_chars=cap,
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
            stop=stop,
            json_directive=json_directive,
            max_chars=cap,
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
        stop=stop,
        json_directive=json_directive,
        max_chars=cap,
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


def _json_retry_request(plan: _Plan, req: ChatRequest, messages: list[dict],
                        max_prompt_chars: int = 18000) -> ChatRequest:
    """A fresh-thread retry that restates the JSON-only contract."""
    base = system_prompt.build_prompt(messages, max_prompt_chars=max_prompt_chars)
    prompt = (f"{base.prompt}\n\n{plan.json_directive}\n\n"
              "Your previous reply was not valid JSON. This reply MUST be a "
              "single valid JSON value and nothing else.")
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
        images_current_only=True,
        system_fingerprint=plan.system_fp,
        page_echo=req.page_echo,
        force_new_thread=True,
        tools=None,
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
    if plan.tools:
        if _needs_tool_retry(text, plan):
            for _ in range(max(0, svc.settings.tool_retries)):
                attempt = _retry_request(plan, req, messages, svc.settings.max_prompt_chars)
                text = await _collect(svc.gateway.chat_stream(attempt))
                if not _needs_tool_retry(text, plan):
                    return text
        return text
    if plan.json_directive:
        for _ in range(max(0, svc.settings.tool_retries)):
            if not _needs_json_retry(text):
                return _extract_json(text) or text
            attempt = _json_retry_request(plan, req, messages, svc.settings.max_prompt_chars)
            text = await _collect(svc.gateway.chat_stream(attempt))
        value = _extract_json(text)
        return value if value is not None else text
    return text


@router.post("/v1/chat/completions")
async def chat_completions(body: ChatCompletionRequest, request: Request,
                           svc: Services = Depends(get_services)):
    spec = resolve_model(body.model, "chat")
    request.state.model = spec.id  # log the actual model, not the client alias
    messages = [m.model_dump() for m in body.messages]
    plan = _build_plan(body, messages, svc.settings)
    if not plan.prompt and not plan.images:
        raise InvalidRequest("messages contain no text or images")
    if plan.json_directive:
        # The directive rides with the first (full) prompt and with every
        # follow-up turn, so JSON mode survives thread reuse.
        plan.prompt = f"{plan.json_directive}\n\n{plan.prompt}"
        if plan.followup_prompt:
            plan.followup_prompt = f"{plan.json_directive}\n\n{plan.followup_prompt}"
    if not body.stream and body.stream_options is not None:
        raise InvalidRequest("stream_options requires stream=true", param="stream_options")
    # Buffered replies (tools / JSON mode) cannot be streamed verbatim: planner
    # JSON has to be rewritten into tool_calls before any chunk is emitted.
    buffered = bool(plan.tools) or bool(plan.json_directive)
    n = body.n or 1

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

    if n > 1:
        # Independent completions: ephemeral threads so the pool spreads them
        # over the connection pool instead of queueing on one conversation.
        async def one() -> str:
            copy = replace(req, conversation_hint=None, cancel=asyncio.Event())
            text = await _collect(svc.gateway.chat_stream(copy))
            return await _retry_nonstream(text, plan, copy, svc, messages)

    if not body.stream and n > 1:
        texts = list(await asyncio.gather(*[one() for _ in range(n)]))
        return _completion(plan, "", completion_id, created, model_name,
                           choices_texts=texts)

    if body.stream and n > 1:
        def chunk_at(index: int, delta: dict, finish: str | None = None) -> dict:
            return {"id": completion_id, "object": "chat.completion.chunk",
                    "created": created, "model": model_name,
                    "system_fingerprint": "muse2api",
                    "choices": [{"index": index, "delta": delta,
                                 "finish_reason": finish}]}

        if buffered:
            # Buffer each choice fully (planner JSON rewritten, JSON extracted),
            # then replay it as standard SSE chunks.
            texts = list(await asyncio.gather(*[one() for _ in range(n)]))

            async def buffered_multi_events() -> AsyncIterator[bytes]:
                try:
                    for index, one_text in enumerate(texts):
                        message, finish = _message_from_text(plan, one_text)
                        if message.get("tool_calls"):
                            calls = message["tool_calls"]
                            yield _sse(chunk_at(index, {"role": "assistant", "content": None,
                                                        "tool_calls": [
                                                            {"index": j, "id": call["id"],
                                                             "type": "function",
                                                             "function": {"name": call["function"]["name"],
                                                                          "arguments": ""}}
                                                            for j, call in enumerate(calls)]}))
                            for j, call in enumerate(calls):
                                yield _sse(chunk_at(index, {"tool_calls": [
                                    {"index": j, "function": {
                                        "arguments": call["function"]["arguments"]}}]}))
                        elif message.get("content"):
                            yield _sse(chunk_at(index, {"role": "assistant",
                                                        "content": message["content"]}))
                        yield _sse(chunk_at(index, {}, finish))
                    if (body.stream_options or {}).get("include_usage"):
                        usage = _usage(plan.prompt, "".join(texts))
                        yield _sse({"id": completion_id, "object": "chat.completion.chunk",
                                    "created": created, "model": model_name,
                                    "system_fingerprint": "muse2api", "choices": [],
                                    "usage": usage})
                except Muse2APIError as exc:
                    yield _sse(exc.to_body())
                except Exception:  # noqa: BLE001
                    log.exception("multi-choice stream crashed")
                    yield _sse({"error": {"message": "internal error",
                                          "type": "server_error",
                                          "param": None, "code": "internal_error"}})
                yield _sse("[DONE]")

            return StreamingResponse(buffered_multi_events(),
                                     media_type="text/event-stream",
                                     headers={"Cache-Control": "no-cache",
                                              "X-Accel-Buffering": "no"})

        # Plain text: true interleaved streaming. One ephemeral upstream turn
        # per choice; deltas are forwarded as they arrive, tagged with the
        # choice index, exactly like api.openai.com's multi-choice streams.
        streams = [svc.gateway.chat_stream(replace(req, conversation_hint=None,
                                                   cancel=asyncio.Event()))
                   for _ in range(n)]
        firsts: list[str | None] = [None] * n

        if svc.settings.stream_heartbeat_interval <= 0:
            # Fail fast: a dead account surfaces as an HTTP error, not a broken
            # event stream. (Heartbeat mode keeps the 200 immediate and waits
            # inside the generator, matching the single-choice behavior.)
            async def _first_of(s) -> str | None:
                try:
                    return await anext(s)
                except StopAsyncIteration:
                    return ""

            firsts = list(await asyncio.gather(*[_first_of(s) for s in streams]))

        async def multi_events() -> AsyncIterator[bytes]:
            q: asyncio.Queue = asyncio.Queue()
            texts: list[str] = [""] * n
            interval = svc.settings.stream_heartbeat_interval
            tasks: list[asyncio.Task] = []
            try:
                if any(f is None for f in firsts):
                    pendings = {i: asyncio.ensure_future(anext(s))
                                for i, s in enumerate(streams) if firsts[i] is None}
                    try:
                        while pendings:
                            done, _ = await asyncio.wait(
                                set(pendings.values()),
                                timeout=interval if interval > 0 else None)
                            if not done and interval > 0:
                                yield b": keepalive\n\n"
                                continue
                            for i, task in list(pendings.items()):
                                if task in done:
                                    try:
                                        firsts[i] = task.result()
                                    except StopAsyncIteration:
                                        firsts[i] = ""
                                    del pendings[i]
                    finally:
                        for task in pendings.values():
                            if not task.done():
                                task.cancel()

                async def pump(i: int, stream, first: str | None) -> None:
                    produced = first or ""
                    try:
                        stop_idx = _first_stop_index(produced, plan.stop)
                        if stop_idx is not None:
                            produced = produced[:stop_idx]
                            texts[i] = produced
                            await q.put(("delta", i, produced))
                            await q.put(("end", i, "stop"))
                            return
                        if plan.max_chars is not None and len(produced) > plan.max_chars:
                            produced = produced[:plan.max_chars]
                            texts[i] = produced
                            await q.put(("delta", i, produced))
                            await q.put(("end", i, "length"))
                            return
                        texts[i] = produced
                        if produced:
                            await q.put(("delta", i, produced))
                        while True:
                            try:
                                delta = await anext(stream)
                            except StopAsyncIteration:
                                await q.put(("end", i, None))
                                return
                            produced += delta
                            stop_idx = _first_stop_index(produced, plan.stop)
                            if stop_idx is not None:
                                already = len(produced) - len(delta)
                                tail = produced[already:stop_idx]
                                if tail:
                                    texts[i] += tail
                                    await q.put(("delta", i, tail))
                                await q.put(("end", i, "stop"))
                                return
                            if plan.max_chars is not None and len(produced) > plan.max_chars:
                                already = len(produced) - len(delta)
                                tail = produced[already:plan.max_chars]
                                if tail:
                                    texts[i] += tail
                                    await q.put(("delta", i, tail))
                                await q.put(("end", i, "length"))
                                return
                            texts[i] += delta
                            await q.put(("delta", i, delta))
                    except asyncio.CancelledError:
                        raise
                    except Muse2APIError as exc:
                        await q.put(("error", i, exc))
                    except Exception:  # noqa: BLE001
                        log.exception("choice %d stream crashed", i)
                        await q.put(("error", i, Muse2APIError("internal error")))
                    finally:
                        with contextlib.suppress(Exception):
                            await stream.aclose()

                tasks[:] = [asyncio.create_task(pump(i, s, f))
                            for i, (s, f) in enumerate(zip(streams, firsts, strict=False))]
                for i in range(n):
                    yield _sse(chunk_at(i, {"role": "assistant", "content": ""}))
                pending = n
                while pending:
                    kind, i, payload = await q.get()
                    if kind == "delta":
                        yield _sse(chunk_at(i, {"content": payload}))
                    elif kind == "end":
                        yield _sse(chunk_at(i, {}, payload or "stop"))
                        pending -= 1
                    else:  # one choice failed; siblings keep streaming
                        yield _sse(payload.to_body())
                        pending -= 1
                if (body.stream_options or {}).get("include_usage"):
                    usage = _usage(plan.prompt, "".join(texts))
                    yield _sse({"id": completion_id, "object": "chat.completion.chunk",
                                "created": created, "model": model_name,
                                "system_fingerprint": "muse2api", "choices": [],
                                "usage": usage})
            except Muse2APIError as exc:
                yield _sse(exc.to_body())
            except Exception:  # noqa: BLE001
                log.exception("multi-choice stream crashed")
                yield _sse({"error": {"message": "internal error",
                                      "type": "server_error",
                                      "param": None, "code": "internal_error"}})
            finally:
                # A client disconnect closes this generator; the pumps must be
                # torn down so the upstream turns get cancelled.
                for t in tasks:
                    t.cancel()
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
            yield _sse("[DONE]")

        return StreamingResponse(multi_events(),
                                 media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    stream = svc.gateway.chat_stream(req)

    if not body.stream:
        text = await _collect(stream)
        text = await _retry_nonstream(text, plan, req, svc, messages)
        return _completion(plan, text, completion_id, created, model_name)

    def chunk(delta: dict, finish: str | None = None) -> dict:
        return {"id": completion_id, "object": "chat.completion.chunk", "created": created,
                "model": model_name, "system_fingerprint": "muse2api",
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
            if plan.json_directive and not plan.tools:
                for _ in range(max(0, svc.settings.tool_retries)):
                    if not _needs_json_retry(produced):
                        break
                    attempt = _json_retry_request(
                        plan, req, messages, svc.settings.max_prompt_chars)
                    holder = []
                    async for piece in _pump_stream(
                            svc.gateway.chat_stream(attempt), interval, holder):
                        yield piece
                    produced = holder[-1] if holder else ""
                produced = _extract_json(produced) or produced
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
                            "created": created, "model": model_name,
                            "system_fingerprint": "muse2api", "choices": [],
                            "usage": usage})
        except Muse2APIError as exc:
            yield _sse(exc.to_body())
        except Exception:  # noqa: BLE001
            log.exception("stream crashed")
            yield _sse({"error": {"message": "internal error", "type": "server_error",
                                  "param": None, "code": "internal_error"}})
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
            finish_reason = "stop"
            stop_idx = _first_stop_index(produced, plan.stop)
            if stop_idx is not None:
                produced = produced[:stop_idx]
            elif plan.max_chars is not None and len(produced) > plan.max_chars:
                produced = produced[:plan.max_chars]
                finish_reason = "length"
            yield _sse(chunk({"role": "assistant", "content": produced}))
            finished_early = stop_idx is not None or finish_reason == "length"
            if not finished_early:
                async for delta in stream:
                    produced += delta
                    stop_idx = _first_stop_index(produced, plan.stop)
                    if stop_idx is not None:
                        # OpenAI semantics: cut before the stop sequence and
                        # end; breaking lets the finally below cancel the
                        # upstream turn.
                        already = len(produced) - len(delta)
                        tail = produced[already:stop_idx] if stop_idx > already else ""
                        if tail:
                            yield _sse(chunk({"content": tail}))
                        produced = produced[:stop_idx]
                        break
                    if plan.max_chars is not None and len(produced) > plan.max_chars:
                        already = len(produced) - len(delta)
                        tail = produced[already:plan.max_chars]
                        if tail:
                            yield _sse(chunk({"content": tail}))
                        produced = produced[:plan.max_chars]
                        finish_reason = "length"
                        break
                    yield _sse(chunk({"content": delta}))
            yield _sse(chunk({}, finish_reason))
            if (body.stream_options or {}).get("include_usage"):
                usage = _usage(plan.prompt, produced)
                yield _sse({"id": completion_id, "object": "chat.completion.chunk",
                            "created": created, "model": model_name,
                            "system_fingerprint": "muse2api", "choices": [],
                            "usage": usage})
        except Muse2APIError as exc:
            yield _sse(exc.to_body())
        except Exception:  # noqa: BLE001
            log.exception("stream crashed")
            yield _sse({"error": {"message": "internal error", "type": "server_error",
                                  "param": None, "code": "internal_error"}})
        finally:
            cancel.set()
            await stream.aclose()
        yield _sse("[DONE]")

    buffered = bool(plan.tools) or bool(plan.json_directive)
    if svc.settings.stream_heartbeat_interval > 0:
        return StreamingResponse(buffered_events(None) if buffered else events(None),
                                 media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    first = await first_delta()
    return StreamingResponse(buffered_events(first) if buffered else events(first),
                             media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _message_from_text(plan: _Plan, text: str) -> tuple[dict, str]:
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
        content = _apply_stop(_clean_text(text), plan.stop)
        if plan.max_chars is not None and len(content) > plan.max_chars:
            content = content[:plan.max_chars]
            finish = "length"
        message = {"role": "assistant", "content": content}
    return message, finish


def _completion(plan: _Plan, text: str,
                completion_id: str, created: int, model_name: str, *,
                choices_texts: list[str] | None = None) -> dict:
    if choices_texts is None:
        message, finish = _message_from_text(plan, text)
        choices = [{"index": 0, "message": message, "finish_reason": finish}]
        usage_text = text
    else:
        choices = []
        for index, one_text in enumerate(choices_texts):
            message, finish = _message_from_text(plan, one_text)
            choices.append({"index": index, "message": message, "finish_reason": finish})
        usage_text = "".join(choices_texts)
    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": model_name,
        "system_fingerprint": "muse2api",
        "choices": choices,
        "usage": _usage(plan.prompt, usage_text),
    }
