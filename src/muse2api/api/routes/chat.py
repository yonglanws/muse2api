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
from ...core.params import validate_metadata
from ...core.prompt import flatten_messages, message_turns
from ...core.tokens import count_tokens, truncate_tokens
from ...drivers.base import ChatRequest, InputImage
from ...errors import InvalidRequest, Muse2APIError, NotFound
from ...services.container import Services
from ..deps import conversation_key, get_services, public_base, require_api_key
from ..schemas import ChatCompletionRequest, ChatCompletionUpdateRequest

log = logging.getLogger(__name__)
router = APIRouter(tags=["chat"], dependencies=[Depends(require_api_key)])


def _approx_tokens(text: str) -> int:
    return count_tokens(text)


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
    parallel_tool_calls: bool = True
    web_search: bool = False
    legacy_functions: bool = False
    stop: list[str] | None = None
    json_directive: str | None = None
    # max_tokens mapped to an approximate character cap (muse.ai exposes no
    # sampling control; truncation is the only honest way to honor the limit).
    max_chars: int | None = None


_WEB_TOOL_TYPES = {
    "web_search",
    "web_search_preview",
    "web_search_2025_08_26",
    "web_search_2025_08_28",
}
_WEB_SEARCH_DIRECTIVE = (
    "[[WEB SEARCH]]\n"
    "You have native web search and page-reading tools. When the answer depends "
    "on current, external or verifiable facts, actually search and open the "
    "pages before answering. Cite the exact source URLs you really read; never "
    "invent a source."
)


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
    if rtype not in (None, "text", "json_object", "json_schema"):
        raise InvalidRequest(
            f"response_format type '{rtype}' is not supported",
            param="response_format")
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
        if len(seqs) > 4:
            raise InvalidRequest("stop may contain at most 4 sequences",
                                 param="stop")
        return seqs or None
    return None


def _cap_from_max_tokens(max_tokens: int | None) -> int | None:
    """``max_tokens`` is a hard completion-token cap.

    The stored value is the token limit itself; reply text is cut at a real BPE
    token boundary (``truncate_tokens``) before usage is reported. Applies to
    plain-text replies only: cutting a tool-call JSON mid-argument would
    corrupt it.
    """
    if max_tokens and max_tokens > 0:
        return max_tokens
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
            if not picked:
                raise InvalidRequest(
                    f"tool_choice references unknown function '{name}'",
                    param="tool_choice")
            return picked, True
    return tools, tool_choice == "required"


def _split_tools(body: ChatCompletionRequest) -> tuple[list[dict] | None, bool]:
    """Separate function tools from built-in web-search tools.

    Anthropic/OpenAI-legacy clients still send ``functions``; newer clients can
    ask for ``web_search`` (Responses) or ``web_search_options`` (Chat). muse.ai
    has a native browser, so web search is a prompt directive returning real
    citations, not an emulated function.
    """
    functions: list[dict] = []
    web_search = body.web_search_options is not None
    for tool in body.tools or []:
        if not isinstance(tool, dict):
            raise InvalidRequest("each tool must be an object", param="tools")
        ttype = tool.get("type")
        if isinstance(ttype, str) and (ttype in _WEB_TOOL_TYPES
                                      or ttype.startswith("web_search")):
            web_search = True
            continue
        fn = tool.get("function")
        if isinstance(fn, dict) and fn.get("name"):
            # Chat Completions shape: {"type":"function","function":{...}}
            functions.append(tool)
            continue
        if ttype == "function" and tool.get("name"):
            # Responses shape: {"type":"function","name":...,"parameters":...}
            functions.append({"type": "function", "function": {
                key: tool[key] for key in
                ("name", "description", "parameters", "strict") if key in tool}})
            continue
        if ttype is None and isinstance(fn, dict):
            functions.append({"type": "function", "function": fn})
            continue
        raise InvalidRequest(
            f"tool type '{ttype}' is not supported by muse models", param="tools")
    for fn in body.functions or []:
        if not isinstance(fn, dict) or not fn.get("name"):
            raise InvalidRequest("each legacy function needs a name", param="functions")
        functions.append({"type": "function", "function": fn})
    return (functions or None), web_search


def _build_plan(body: ChatCompletionRequest, messages: list[dict], settings) -> _Plan:
    raw_tools, web_search = _split_tools(body)
    legacy_functions = bool(body.functions) and not body.tools
    tool_choice = body.tool_choice
    if tool_choice is None and body.function_call is not None:
        # Legacy function_call → modern tool_choice.
        if body.function_call == "none":
            tool_choice = "none"
        elif body.function_call == "auto":
            tool_choice = "auto"
        elif isinstance(body.function_call, dict) and body.function_call.get("name"):
            tool_choice = {"type": "function",
                           "function": {"name": body.function_call["name"]}}
    tools, force_tool = _select_tools(raw_tools, tool_choice)
    if not settings.emulate_tools:
        tools, force_tool = None, False
    # JSON mode rides on a prompt directive; with tools the reply is a tool
    # call anyway, so the directive would only corrupt it.
    json_directive = None if tools else _json_directive(body.response_format)
    stop = _stop_list(body.stop)
    cap = _cap_from_max_tokens(
        body.max_completion_tokens
        if body.max_completion_tokens is not None else body.max_tokens)
    parallel = body.parallel_tool_calls is not False

    if tools:
        plan = system_prompt.build_tool_prompt(
            messages,
            tools,
            max_prompt_chars=settings.max_prompt_chars,
            force_tool=force_tool,
            native=settings.tool_channel != "planner",
            web_search=web_search,
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
            parallel_tool_calls=parallel,
            web_search=web_search,
            legacy_functions=legacy_functions,
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
            parallel_tool_calls=parallel,
            web_search=web_search,
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
        parallel_tool_calls=parallel,
        web_search=web_search,
        stop=stop,
        json_directive=json_directive,
        max_chars=cap,
    )


def _clean_text(text: str) -> str:
    return tool_plan.strip_text_prefix(text or "").strip()


def _limit_tool_calls(plan: _Plan, calls):
    """``parallel_tool_calls: false`` means at most one call per turn."""
    if calls and plan.parallel_tool_calls is False:
        return calls[:1]
    return calls


def _usage(prompt_text: str, completion_text: str) -> dict:
    p, c = _approx_tokens(prompt_text), _approx_tokens(completion_text)
    return {
        "prompt_tokens": p,
        "completion_tokens": c,
        "total_tokens": p + c,
        "prompt_tokens_details": {"cached_tokens": 0, "audio_tokens": 0},
        "completion_tokens_details": {
            "reasoning_tokens": 0, "audio_tokens": 0,
            "accepted_prediction_tokens": 0,
            "rejected_prediction_tokens": 0,
        },
    }


def _message_usage_text(message: dict) -> str:
    """Completion text used for usage: final content or serialized calls."""
    content = message.get("content")
    if isinstance(content, str):
        return content
    calls = message.get("tool_calls")
    if calls:
        return json.dumps(calls, ensure_ascii=False)
    call = message.get("function_call")
    return json.dumps(call, ensure_ascii=False) if call else ""


def _media_suffix(media, svc, base: str) -> str:
    """Render generated chat artifacts as markdown, like muse.ai's inline cards.

    ``muse.ai`` puts images/videos/documents directly into the assistant bubble;
    an OpenAI text reply has no attachment channel, so a markdown block pointing
    at the gateway's media route is the faithful equivalent (clients that render
    markdown show it inline, text-only clients still see the URL).
    """
    blocks: list[str] = []
    for item in media or []:
        name = svc.media.save(item.data, item.mime, prefix="chat")
        url = f"{base}/v1/media/{name}"
        label = " ".join(str(item.label or item.kind or "attachment").split())[:80]
        label = label.replace("[", "(").replace("]", ")").replace("(", "（").replace(")", "）")
        blocks.append(f"![{label}]({url})" if item.kind == "image"
                      else f"[{label}]({url})")
    return ("\n\n" + "\n\n".join(blocks)) if blocks else ""



def _reply_suffix(plan: _Plan, media, svc, base: str) -> str:
    """Media links are Markdown; never append them to JSON-mode output."""
    return "" if plan.json_directive else _media_suffix(media, svc, base)

def _chat_annotations(content: str, citations) -> list[dict] | None:
    """OpenAI ``message.annotations`` entries (the web-search citation shape)."""
    if not citations:
        return None
    out: list[dict] = []
    seen: set[str] = set()
    for citation in citations:
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
        out.append({"type": "url_citation",
                    "url_citation": {"url": url, "title": title or url,
                                     "start_index": start, "end_index": end}})
    return out or None


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
        media=req.media,
        citations=req.citations,
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
        media=req.media,
        citations=req.citations,
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
    owner = getattr(request.state, "key_id", None)
    # file_id references (OpenAI Files API) become inline data URLs here; the
    # rest of the pipeline already understands images/file_ref attachments.
    source_messages = [m.model_dump() for m in body.messages]
    messages = svc.files.expand_messages(source_messages, owner)
    plan = _build_plan(body, messages, svc.settings)
    if not plan.prompt and not plan.images:
        raise InvalidRequest("messages contain no text or images")
    if plan.json_directive:
        # The directive rides with the first (full) prompt and with every
        # follow-up turn, so JSON mode survives thread reuse.
        plan.prompt = f"{plan.json_directive}\n\n{plan.prompt}"
        if plan.followup_prompt:
            plan.followup_prompt = f"{plan.json_directive}\n\n{plan.followup_prompt}"
    if plan.web_search:
        # Same for built-in web search: muse.ai browses natively, and the
        # citations it reads are returned as standard url_citation annotations.
        plan.prompt = f"{_WEB_SEARCH_DIRECTIVE}\n\n{plan.prompt}"
        if plan.followup_prompt:
            plan.followup_prompt = f"{_WEB_SEARCH_DIRECTIVE}\n\n{plan.followup_prompt}"
    if body.logprobs or body.top_logprobs is not None:
        raise InvalidRequest(
            "logprobs are not supported by muse models", param="logprobs")
    if body.temperature is not None and not 0 <= body.temperature <= 2:
        raise InvalidRequest("temperature must be between 0 and 2",
                             param="temperature")
    if body.top_p is not None and not 0 <= body.top_p <= 1:
        raise InvalidRequest("top_p must be between 0 and 1", param="top_p")
    for penalty in ("presence_penalty", "frequency_penalty"):
        value = getattr(body, penalty, None)
        if value is not None and not -2 <= value <= 2:
            raise InvalidRequest(f"{penalty} must be between -2 and 2",
                                 param=penalty)
    if body.modalities and "audio" in body.modalities:
        raise InvalidRequest(
            "audio output is not supported; muse models reply in text only",
            param="modalities")
    if body.audio is not None:
        raise InvalidRequest(
            "audio output is not supported; muse models reply in text only",
            param="audio")
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
    if body.service_tier not in (None, "auto", "default", "flex",
                                     "scale", "priority", "fast"):
        raise InvalidRequest(
            f"service_tier '{body.service_tier}' is not available for muse models",
            param="service_tier")
    service_tier = "default"
    body.metadata = validate_metadata(body.metadata)

    if n > 1:
        # Independent completions: ephemeral threads so the pool spreads them
        # over the connection pool instead of queueing on one conversation.
        # Each choice gets its own media/citation sink: the driver fills these
        # while the choice runs, and they are rendered per choice below.
        reqs = [replace(req, conversation_hint=None, cancel=asyncio.Event(),
                        media=[], citations=[])
                for _ in range(n)]

        async def one(index: int) -> str:
            copy = reqs[index]
            text = await _collect(svc.gateway.chat_stream(copy))
            return await _retry_nonstream(text, plan, copy, svc, messages)

    if not body.stream and n > 1:
        texts = list(await asyncio.gather(*[one(i) for i in range(n)]))
        base = public_base(request)
        completion = _completion(plan, "", completion_id, created, model_name,
                                 choices_texts=texts,
                                 suffixes=[_reply_suffix(plan, r.media, svc, base) for r in reqs],
                                 citations=[r.citations for r in reqs],
                                 service_tier=service_tier, metadata=body.metadata)
        _save_stored_completion(svc, body, completion, source_messages, owner)
        return completion

    if body.stream and n > 1:
        include_usage = bool((body.stream_options or {}).get("include_usage"))
        base = public_base(request)

        def chunk_at(index: int, delta: dict, finish: str | None = None) -> dict:
            obj = {"id": completion_id, "object": "chat.completion.chunk",
                   "created": created, "model": model_name,
                   "system_fingerprint": "muse2api", "service_tier": service_tier,
                   "choices": [{"index": index, "delta": delta,
                                "logprobs": None,
                                "finish_reason": finish}]}
            if include_usage:
                obj["usage"] = None
            return obj

        def choice_text(index: int, one_text: str, suffix: str) -> tuple[dict, str]:
            message, finish = _message_from_text(plan, one_text)
            if suffix:
                message["content"] = (message.get("content") or "") + suffix
            annotations = _chat_annotations(message.get("content") or "",
                                            reqs[index].citations)
            if annotations:
                message["annotations"] = annotations
            return message, finish

        if buffered:
            # Buffer each choice fully (planner JSON rewritten, JSON extracted),
            # then replay it as standard SSE chunks.
            texts = list(await asyncio.gather(*[one(i) for i in range(n)]))

            async def buffered_multi_events() -> AsyncIterator[bytes]:
                try:
                    stored_choices: list[dict] = []
                    for index, one_text in enumerate(texts):
                        message, finish = choice_text(
                            index, one_text,
                            _reply_suffix(plan, reqs[index].media, svc, base))
                        message.setdefault("refusal", None)
                        message.setdefault("annotations", [])
                        message.setdefault("audio", None)
                        stored_choices.append({"index": index, "message": message,
                                               "logprobs": None,
                                               "finish_reason": finish})
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
                        finish_delta = {}
                        if message.get("annotations"):
                            finish_delta["annotations"] = message["annotations"]
                        yield _sse(chunk_at(index, finish_delta, finish))
                    _save_stored_completion(
                        svc, body,
                        _completion_payload(plan, stored_choices,
                                            "".join(c["message"].get("content") or ""
                                                    for c in stored_choices),
                                            completion_id, created, model_name,
                                            service_tier, body.metadata),
                        source_messages, owner)
                    if include_usage:
                        usage = _usage(plan.prompt, "".join(
                            _message_usage_text(c["message"]) for c in stored_choices))
                        yield _sse({"id": completion_id, "object": "chat.completion.chunk",
                                    "created": created, "model": model_name,
                                    "system_fingerprint": "muse2api",
                                    "service_tier": service_tier, "choices": [],
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
        streams = [svc.gateway.chat_stream(r) for r in reqs]
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
            finishes: list[str] = ["stop"] * n
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
                        if plan.max_chars is not None and count_tokens(produced) > plan.max_chars:
                            produced = truncate_tokens(produced, plan.max_chars)
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
                            if plan.max_chars is not None and count_tokens(produced) > plan.max_chars:
                                already = len(produced) - len(delta)
                                truncated = truncate_tokens(produced, plan.max_chars)
                                tail = truncated[already:] if len(truncated) > already else ""
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
                failures: set[int] = set()
                stored_choices: list[dict] = []
                while pending:
                    kind, i, payload = await q.get()
                    if kind == "delta":
                        yield _sse(chunk_at(i, {"content": payload}))
                    elif kind == "end":
                        finishes[i] = payload or "stop"
                        suffix = _reply_suffix(plan, reqs[i].media, svc, base)
                        if suffix:
                            texts[i] += suffix
                            yield _sse(chunk_at(i, {"content": suffix}))
                        annotations = _chat_annotations(texts[i], reqs[i].citations)
                        finish_delta = {"annotations": annotations} if annotations else {}
                        yield _sse(chunk_at(i, finish_delta, payload or "stop"))
                        pending -= 1
                    else:  # one choice failed; siblings keep streaming
                        failures.add(i)
                        yield _sse(payload.to_body())
                        pending -= 1
                if not failures:
                    stored_choices = []
                    for i in range(n):
                        message = {"role": "assistant", "content": texts[i],
                                   "refusal": None, "annotations": [], "audio": None}
                        annotations = _chat_annotations(texts[i], reqs[i].citations)
                        if annotations:
                            message["annotations"] = annotations
                        stored_choices.append({"index": i, "message": message,
                                               "logprobs": None,
                                               "finish_reason": finishes[i]})
                    _save_stored_completion(
                        svc, body,
                        _completion_payload(plan, stored_choices, "".join(texts),
                                            completion_id, created, model_name,
                                            service_tier, body.metadata),
                        source_messages, owner)
                if include_usage:
                    usage = _usage(plan.prompt, "".join(
                            _message_usage_text(c["message"]) for c in stored_choices))
                    yield _sse({"id": completion_id, "object": "chat.completion.chunk",
                                "created": created, "model": model_name,
                                "system_fingerprint": "muse2api",
                                "service_tier": service_tier, "choices": [],
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
    base = public_base(request)
    include_usage = bool((body.stream_options or {}).get("include_usage"))

    if not body.stream:
        text = await _collect(stream)
        text = await _retry_nonstream(text, plan, req, svc, messages)
        completion = _completion(plan, text, completion_id, created, model_name,
                                 suffixes=[_reply_suffix(plan, req.media, svc, base)],
                                 citations=[req.citations],
                                 service_tier=service_tier, metadata=body.metadata)
        _save_stored_completion(svc, body, completion, source_messages, owner)
        return completion

    def chunk(delta: dict, finish: str | None = None) -> dict:
        obj = {"id": completion_id, "object": "chat.completion.chunk", "created": created,
               "model": model_name, "system_fingerprint": "muse2api",
               "service_tier": service_tier,
               "choices": [{"index": 0, "delta": delta, "logprobs": None,
                            "finish_reason": finish}]}
        if include_usage:
            obj["usage"] = None
        return obj

    async def first_delta() -> str | None:
        """Prefetch the first upstream delta so failures become HTTP errors."""
        try:
            return await anext(stream)
        except StopAsyncIteration:
            return ""

    async def buffered_events(first: str | None) -> AsyncIterator[bytes]:
        """Buffer a tool-enabled reply so planner JSON can be rewritten."""
        produced = ""
        # Planner turns can take a minute; keep the SSE connection warm even
        # when the global heartbeat is disabled.
        interval = svc.settings.stream_heartbeat_interval or 15.0
        try:
            # Commit a normal assistant chunk immediately: planner JSON needs a
            # full turn, but the client should not wait without any SSE data.
            yield _sse(chunk({"role": "assistant", "content": ""}))
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
            calls = _limit_tool_calls(plan, calls)
            if calls is None and plan.tools:
                tool, args, reply = tool_plan.parse_tool_plan(produced, plan.tools)
            if calls:
                encoded = [tool_plan.make_tool_call(name, args or {})
                           for name, args in calls]
                if plan.legacy_functions:
                    call = encoded[0]
                    stored_message = {
                        "role": "assistant", "content": None,
                        "function_call": {
                            "name": call["function"]["name"],
                            "arguments": call["function"]["arguments"]}}
                    stored_finish = "function_call"
                    yield _sse(chunk({"content": None, "function_call": {
                        "name": call["function"]["name"], "arguments": ""}}))
                    yield _sse(chunk({"function_call": {
                        "arguments": call["function"]["arguments"]}}))
                    yield _sse(chunk({}, "function_call"))
                else:
                    stored_message = {"role": "assistant", "content": None,
                                      "tool_calls": encoded}
                    stored_finish = "tool_calls"
                    yield _sse(chunk({"content": None, "tool_calls": [
                        {"index": i, "id": call["id"], "type": "function",
                         "function": {"name": call["function"]["name"],
                                      "arguments": ""}}
                        for i, call in enumerate(encoded)
                    ]}))
                    for i, call in enumerate(encoded):
                        yield _sse(chunk({"tool_calls": [
                            {"index": i, "function": {
                                "arguments": call["function"]["arguments"]}}
                        ]}))
                    yield _sse(chunk({}, "tool_calls"))
            elif tool:
                call = tool_plan.make_tool_call(tool, args or {})
                if plan.legacy_functions:
                    stored_message = {
                        "role": "assistant", "content": None,
                        "function_call": {
                            "name": call["function"]["name"],
                            "arguments": call["function"]["arguments"]}}
                    stored_finish = "function_call"
                    yield _sse(chunk({"content": None, "function_call": {
                        "name": call["function"]["name"], "arguments": ""}}))
                    yield _sse(chunk({"function_call": {
                        "arguments": call["function"]["arguments"]}}))
                    yield _sse(chunk({}, "function_call"))
                else:
                    stored_message = {"role": "assistant", "content": None,
                                      "tool_calls": [call]}
                    stored_finish = "tool_calls"
                    yield _sse(chunk({"content": None, "tool_calls": [
                        {"index": 0, "id": call["id"], "type": "function",
                         "function": {"name": call["function"]["name"],
                                      "arguments": ""}}
                    ]}))
                    yield _sse(chunk({"tool_calls": [
                        {"index": 0, "function": {
                            "arguments": call["function"]["arguments"]}}
                    ]}))
                    yield _sse(chunk({}, "tool_calls"))
            else:
                cleaned = _clean_text(reply if reply is not None else produced)
                suffix = _reply_suffix(plan, req.media, svc, base)
                cleaned += suffix
                annotations = _chat_annotations(cleaned, req.citations)
                stored_message = {"role": "assistant", "content": cleaned}
                if annotations:
                    stored_message["annotations"] = annotations
                stored_finish = "stop"
                for i in range(0, len(cleaned), 24):
                    yield _sse(chunk({"content": cleaned[i:i + 24]}))
                yield _sse(chunk({"annotations": annotations} if annotations else {}, "stop"))
            stored_message.setdefault("refusal", None)
            stored_message.setdefault("annotations", [])
            stored_message.setdefault("audio", None)
            _save_stored_completion(
                svc, body,
                _completion_payload(plan, [{"index": 0, "message": stored_message,
                                            "logprobs": None,
                                            "finish_reason": stored_finish}],
                                    produced, completion_id, created, model_name,
                                    service_tier, body.metadata),
                source_messages, owner)
            if include_usage:
                usage = _usage(plan.prompt, _message_usage_text(stored_message))
                yield _sse({"id": completion_id, "object": "chat.completion.chunk",
                            "created": created, "model": model_name,
                            "system_fingerprint": "muse2api",
                            "service_tier": service_tier, "choices": [],
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
            elif plan.max_chars is not None and count_tokens(produced) > plan.max_chars:
                produced = truncate_tokens(produced, plan.max_chars)
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
                    if plan.max_chars is not None and count_tokens(produced) > plan.max_chars:
                        already = len(produced) - len(delta)
                        truncated = truncate_tokens(produced, plan.max_chars)
                        tail = truncated[already:] if len(truncated) > already else ""
                        if tail:
                            yield _sse(chunk({"content": tail}))
                        produced = truncated
                        finish_reason = "length"
                        break
                    yield _sse(chunk({"content": delta}))
            suffix = _reply_suffix(plan, req.media, svc, base)
            if suffix:
                produced += suffix
                yield _sse(chunk({"content": suffix}))
            annotations = _chat_annotations(produced, req.citations)
            yield _sse(chunk({"annotations": annotations} if annotations else {},
                             finish_reason))
            stored_message = {"role": "assistant", "content": produced,
                              "refusal": None, "annotations": annotations or [],
                              "audio": None}
            _save_stored_completion(
                svc, body,
                _completion_payload(plan, [{"index": 0, "message": stored_message,
                                            "logprobs": None,
                                            "finish_reason": finish_reason}],
                                    produced, completion_id, created, model_name,
                                    service_tier, body.metadata),
                source_messages, owner)
            if include_usage:
                usage = _usage(plan.prompt, _message_usage_text(stored_message))
                yield _sse({"id": completion_id, "object": "chat.completion.chunk",
                            "created": created, "model": model_name,
                            "system_fingerprint": "muse2api",
                            "service_tier": service_tier, "choices": [],
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

    if buffered:
        # Send the 200 and the assistant role chunk immediately; the planner
        # needs a full turn before the tool_calls can be replayed.
        return StreamingResponse(buffered_events(None),
                                 media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    first = await first_delta()
    return StreamingResponse(events(first),
                             media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _completion_payload(plan: _Plan, choices: list[dict], usage_text: str,
                        completion_id: str, created: int, model_name: str,
                        service_tier: str, metadata: dict | None) -> dict:
    """Build the ChatCompletion object stored for ``store: true`` requests."""
    payload = {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": model_name,
        "service_tier": service_tier,
        "system_fingerprint": "muse2api",
        "choices": choices,
        "usage": _usage(plan.prompt, usage_text),
    }
    if metadata is not None:
        payload["metadata"] = metadata
    return payload


def _stored_input_messages(completion_id: str, messages: list[dict]) -> list[dict]:
    """The request messages in OpenAI's ChatCompletionStoreMessage shape.

    The messages endpoint returns the *input* messages; the generated reply is
    part of the completion object itself. ``content_parts`` keeps multimodal
    parts while ``content`` mirrors the concatenated text, as upstream does.
    """
    stored = []
    for index, message in enumerate(messages):
        content = message.get("content")
        if isinstance(content, list):
            text = "".join(
                part.get("text") or ""
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            )
            parts = content
        else:
            text = content
            parts = None
        stored.append({
            "id": f"{completion_id}-{index}",
            "role": message.get("role") or "user",
            "content": text,
            "name": message.get("name"),
            "content_parts": parts,
        })
        for key in ("tool_calls", "function_call", "refusal", "audio",
                    "tool_call_id"):
            if message.get(key) is not None:
                stored[-1][key] = message[key]
    return stored


def _save_stored_completion(svc: Services, body: ChatCompletionRequest,
                            completion: dict, source_messages: list[dict],
                            owner: str | None = None) -> None:
    if not body.store:
        return
    try:
        svc.completions.save(
            completion, _stored_input_messages(completion["id"], source_messages),
            metadata=body.metadata, user=body.user, owner=owner)
    except Exception:  # noqa: BLE001
        # Storing is a convenience; a full disk must not lose the reply.
        log.exception("failed to persist stored chat completion")


def _query_order(request: Request) -> str:
    order = request.query_params.get("order", "asc")
    if order not in ("asc", "desc"):
        raise InvalidRequest("order must be 'asc' or 'desc'", param="order")
    return order


def _query_limit(request: Request) -> int:
    raw = request.query_params.get("limit")
    if raw is None:
        return 20
    try:
        limit = int(raw)
    except ValueError:
        raise InvalidRequest("limit must be an integer", param="limit") from None
    if not 1 <= limit <= 100:
        raise InvalidRequest("limit must be between 1 and 100", param="limit")
    return limit


def _query_metadata(request: Request) -> dict[str, str]:
    filters: dict[str, str] = {}
    for key, value in request.query_params.multi_items():
        match = re.fullmatch(r"metadata\[(.+)]", key)
        if match:
            filters[match.group(1)] = value
    return filters


def _missing_completion(completion_id: str) -> NotFound:
    return NotFound(f"No chat completion found with id '{completion_id}'.")


@router.get("/v1/chat/completions")
async def list_chat_completions(request: Request,
                                svc: Services = Depends(get_services)) -> dict:
    try:
        return svc.completions.list(
            model=request.query_params.get("model") or None,
            metadata=_query_metadata(request),
            after=request.query_params.get("after") or None,
            limit=_query_limit(request),
            order=_query_order(request),
            owner=getattr(request.state, "key_id", None),
        )
    except ValueError as exc:
        raise InvalidRequest(str(exc)) from exc


@router.get("/v1/chat/completions/{completion_id}")
async def retrieve_chat_completion(completion_id: str, request: Request,
                                   svc: Services = Depends(get_services)) -> dict:
    completion = svc.completions.get(
        completion_id, getattr(request.state, "key_id", None))
    if completion is None:
        raise _missing_completion(completion_id)
    return completion


@router.post("/v1/chat/completions/{completion_id}")
async def update_chat_completion(completion_id: str, body: ChatCompletionUpdateRequest,
                                 request: Request,
                                 svc: Services = Depends(get_services)) -> dict:
    completion = svc.completions.update_metadata(
        completion_id, body.metadata, getattr(request.state, "key_id", None))
    if completion is None:
        raise _missing_completion(completion_id)
    return completion


@router.delete("/v1/chat/completions/{completion_id}")
async def delete_chat_completion(completion_id: str, request: Request,
                                 svc: Services = Depends(get_services)) -> dict:
    if not svc.completions.delete(
            completion_id, getattr(request.state, "key_id", None)):
        raise _missing_completion(completion_id)
    return {"id": completion_id, "object": "chat.completion.deleted", "deleted": True}


@router.get("/v1/chat/completions/{completion_id}/messages")
async def list_chat_completion_messages(completion_id: str, request: Request,
                                        svc: Services = Depends(get_services)) -> dict:
    try:
        page = svc.completions.messages(
            completion_id,
            after=request.query_params.get("after") or None,
            limit=_query_limit(request),
            order=_query_order(request),
            owner=getattr(request.state, "key_id", None),
        )
    except ValueError as exc:
        raise InvalidRequest(str(exc)) from exc
    if page is None:
        raise _missing_completion(completion_id)
    return page


def _message_from_text(plan: _Plan, text: str) -> tuple[dict, str]:
    message: dict = {}
    finish = "stop"
    if plan.tools:
        calls = _limit_tool_calls(plan, tool_plan.parse_tool_calls_list(text, plan.tools))
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
        if plan.max_chars is not None and count_tokens(content) > plan.max_chars:
            content = truncate_tokens(content, plan.max_chars)
            finish = "length"
        message = {"role": "assistant", "content": content}
    if plan.legacy_functions and message.get("tool_calls"):
        call = message["tool_calls"][0]
        message = {"role": "assistant", "content": None,
                   "function_call": {"name": call["function"]["name"],
                                     "arguments": call["function"]["arguments"]}}
        finish = "function_call"
    return message, finish


def _completion(plan: _Plan, text: str,
                completion_id: str, created: int, model_name: str, *,
                choices_texts: list[str] | None = None,
                suffixes: list[str] | None = None,
                citations: list[list] | None = None,
                service_tier: str = "default",
                metadata: dict | None = None) -> dict:
    if choices_texts is None:
        message, finish = _message_from_text(plan, text)
        content = message.get("content") or ""
        has_calls = bool(message.get("tool_calls") or message.get("function_call"))
        suffix = "" if has_calls else (suffixes or [""])[0]
        if suffix:
            content += suffix
            message["content"] = content
        annotations = (_chat_annotations(content, (citations or [[]])[0])
                       if content else None)
        message.setdefault("refusal", None)
        message.setdefault("annotations", annotations or [])
        message.setdefault("audio", None)
        if annotations:
            message["annotations"] = annotations
        choices = [{"index": 0, "message": message, "logprobs": None,
                    "finish_reason": finish}]
        usage_text = _message_usage_text(message)
    else:
        choices = []
        for index, one_text in enumerate(choices_texts):
            message, finish = _message_from_text(plan, one_text)
            content = message.get("content") or ""
            has_calls = bool(message.get("tool_calls") or message.get("function_call"))
            suffix = ("" if has_calls else
                      (suffixes or [])[index] if suffixes and index < len(suffixes) else "")
            if suffix:
                content += suffix
                message["content"] = content
            annotations = (
                _chat_annotations(
                    content, citations[index] if citations and index < len(citations) else [])
                if content else None)
            message.setdefault("refusal", None)
            message.setdefault("annotations", annotations or [])
            message.setdefault("audio", None)
            if annotations:
                message["annotations"] = annotations
            choices.append({"index": index, "message": message, "logprobs": None,
                            "finish_reason": finish})
        usage_text = "".join(_message_usage_text(c["message"])
                              for c in choices)
    body = {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": model_name,
        "service_tier": service_tier,
        "system_fingerprint": "muse2api",
        "choices": choices,
        "usage": _usage(plan.prompt, usage_text),
    }
    if metadata is not None:
        body["metadata"] = metadata
    return body
