"""Helpers for OpenAI tool calling over a text-only upstream.

muse.ai cannot execute client-side tools itself. The gateway has two paths:

* the native client-command channel (``drivers/protocol/tools`` logic), where the
  model calls a registered command and the driver captures ``client.invoke``; and
* a flat JSON planner fallback, where the prompt asks for
  ``{"tool": ..., "arguments": ...}`` and the gateway re-encodes it into the
  standard OpenAI ``tool_calls`` shape.

This module contains the format helpers shared by both paths. It knows nothing
about any particular chat platform.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

_TOOL_REFUSAL_RE = re.compile(
    r"(没有[^。！？!?\n]{0,30}(?:这个|该|此)?(?:工具|命令)|"
    r"(?:do not|don't|cannot|can't|unable to)[^.\n]{0,40}(?:tool|command)|"
    r"(?:tool|command)[^.\n]{0,20}(?:not available|not registered)|"
    r"没有接入|没有(?:真正)?(?:可用|实际)?的?工具|无法调用|不能调用|"
    r"工具定义(?:只是|跟着|是文字)|不是我(?:真正)?能调用|"
    r"查不了|查不到|做不了|不能(?:帮你)?(?:查|执行)|"
    r"没有[^。！？!?\n]{0,40}?(?:工具|命令)[^。！？!?\n]{0,12}"
    r"(?:可以用|可调用|可用|被?注册|被?声明|匹配)|"
    r"没有[^。！？!?\n]{0,40}?`[^`]+`[^。！？!?\n]{0,20}?(?:工具|命令)|"
    r"(?:命令|工具)没有(?:真的|被|真正)?(?:注册|声明|匹配)|没有真的给我注册|"
    r"没有(?:真的|被|真正)?声明[^。！？!?\n]{0,12}(?:命令|工具)|"
    r"没有\s*`[^`]+`\s*(?:这个|这样的?)(?:工具|命令)|"
    r"capabilities_not_declared)",
    re.IGNORECASE,
)

_TOOL_RULES = """【工具调用】
如果你需要调用工具，只输出一个扁平 JSON 对象：
{"tool":"工具名","arguments":{...}}
如果不需要工具，只输出：
{"tool":"NONE","reply":"给用户的正常回复"}
arguments 必须是扁平参数对象，不要嵌套 tool_calls，不要输出解释或 Markdown。"""


def tool_specs(tools: list[dict[str, Any]] | None, *, compact: bool = False) -> str:
    """Render OpenAI tool schemas for a prompt."""
    if not tools:
        return ""
    lines: list[str] = []
    for tool in tools:
        fn = tool.get("function") if isinstance(tool, dict) else None
        if not isinstance(fn, dict):
            continue
        name = str(fn.get("name") or "")
        if not name:
            continue
        desc = str(fn.get("description") or "").strip()
        params = fn.get("parameters") if isinstance(fn.get("parameters"), dict) else {}
        if compact:
            props = list((params.get("properties") or {}).keys())
            suffix = f"（参数：{', '.join(props)}）" if props else ""
            lines.append(f"- {name}：{desc[:100]}{suffix}")
        else:
            lines.append(json.dumps(
                {"name": name, "description": desc, "parameters": params},
                ensure_ascii=False,
            ))
    return "\n".join(lines)


def tool_rules(*, force: bool = False) -> str:
    if not force:
        return _TOOL_RULES
    return _TOOL_RULES + "\n本轮必须调用工具：tool 不能是 NONE，只输出 JSON，不要直接回复。"


def tool_protocol_text(tools: list[dict[str, Any]] | None, *, compact: bool = False,
                       force_tool: bool = False) -> str:
    """The tool block appended to a plain prompt."""
    if not tools:
        return ""
    specs = tool_specs(tools, compact=compact)
    if not specs:
        return ""
    return f"【可用工具】\n{specs}\n\n{tool_rules(force=force_tool)}"


def looks_like_tool_refusal(text: str) -> bool:
    """True when the model explained why it cannot call the tools."""
    return bool(text and _TOOL_REFUSAL_RE.search(text))


def _normalize_arguments(tool: str, arguments: dict[str, Any],
                         tools: list[dict[str, Any]]) -> dict[str, Any]:
    """Coerce planner scalars to the JSON types declared by the tool schema."""
    schema: dict[str, Any] = {}
    for item in tools:
        fn = item.get("function") if isinstance(item, dict) else None
        if isinstance(fn, dict) and fn.get("name") == tool:
            params = fn.get("parameters")
            if isinstance(params, dict) and isinstance(params.get("properties"), dict):
                schema = params["properties"]
            break
    out = dict(arguments)
    for key, value in list(out.items()):
        spec = schema.get(key)
        if not isinstance(spec, dict):
            continue
        kind = spec.get("type")
        if kind == "string" and isinstance(value, (int, float, bool)):
            out[key] = str(value)
        elif kind == "integer" and isinstance(value, str) and re.fullmatch(r"-?\d+", value):
            out[key] = int(value)
        elif kind == "number" and isinstance(value, str):
            try:
                out[key] = float(value)
            except ValueError:
                pass
        elif kind == "boolean" and isinstance(value, str) and value.lower() in ("true", "false"):
            out[key] = value.lower() == "true"
    return out


def make_tool_call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": f"call_{uuid.uuid4().hex[:24]}",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
    }


def _allowed_names(tools: list[dict[str, Any]] | None) -> set[str]:
    allowed: set[str] = set()
    for tool in tools or []:
        fn = tool.get("function") if isinstance(tool, dict) else None
        if isinstance(fn, dict) and fn.get("name"):
            allowed.add(str(fn["name"]))
    return allowed


def _json_body(text: str) -> dict[str, Any] | None:
    body = text.strip()
    if body.startswith("```"):
        body = re.sub(r"^```(?:json)?\s*", "", body)
        body = re.sub(r"\s*```$", "", body)
    start, end = body.find("{"), body.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(body[start:end + 1])
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def parse_tool_calls(text: str, tools: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    """Parse an emulated tool-call reply into OpenAI ``tool_calls`` entries."""
    allowed = _allowed_names(tools)
    if not text or not allowed:
        return None
    data = _json_body(text)
    if data is None:
        return None
    raw = data.get("tool_calls")
    if raw is None and ("name" in data or "function" in data):
        raw = [data]
    if not isinstance(raw, list) or not raw:
        return None
    calls: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            return None
        fn = item.get("function") if isinstance(item.get("function"), dict) else item
        name = fn.get("name")
        if not name or str(name) not in allowed:
            return None
        args = fn.get("arguments", item.get("arguments", {}))
        if args is None:
            args = {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except (ValueError, TypeError):
                return None
        if not isinstance(args, dict):
            return None
        calls.append({
            "id": f"call_{uuid.uuid4().hex[:24]}",
            "type": "function",
            "function": {"name": str(name), "arguments": json.dumps(args, ensure_ascii=False)},
        })
    return calls or None


def parse_tool_plan(
    text: str, tools: list[dict[str, Any]] | None
) -> tuple[str | None, dict[str, Any] | None, str | None]:
    """Parse a flat planner answer into ``(tool, arguments, reply)``.

    ``(None, None, None)`` means the text is not a planner answer at all.
    """
    allowed = _allowed_names(tools)
    if not text or not allowed:
        return None, None, None
    data = _json_body(text)
    if data is None:
        return None, None, None
    if isinstance(data.get("tool_calls"), list):
        calls = parse_tool_calls(text, tools)
        if calls:
            args = json.loads(calls[0]["function"]["arguments"])
            return calls[0]["function"]["name"], args, None
        return None, None, None
    tool = data.get("tool", data.get("name"))
    reply = data.get("reply", data.get("content", data.get("text")))
    if tool is None:
        return (None, None, str(reply)) if reply is not None else (None, None, None)
    if not isinstance(tool, str):
        return None, None, None
    if not tool or tool.strip().upper() == "NONE":
        return (None, None, str(reply)) if reply is not None else (None, None, "")
    if tool not in allowed:
        return None, None, None
    args = data.get("arguments", {})
    if args is None:
        args = {}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except (ValueError, TypeError):
            return None, None, None
    if not isinstance(args, dict):
        return None, None, None
    args = _normalize_arguments(tool, args, tools or [])
    return tool, args, (str(reply) if reply is not None else None)


def strip_text_prefix(text: str) -> str:
    """Remove a leading ``TEXT:`` marker some models add."""
    cleaned = text.lstrip()
    if cleaned.upper().startswith("TEXT:"):
        return cleaned[5:].lstrip()
    return text
