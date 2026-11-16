"""Harness-file policy and multi tool-call parsing tests (0.7.0)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from muse2api.core import tool_plan
from muse2api.drivers.protocol.harness import (
    MANAGED_BEGIN,
    MANAGED_END,
    HarnessPolicy,
    desired_content,
)

TOOLS = [
    {"type": "function", "function": {
        "name": "get_weather",
        "description": "Weather",
        "parameters": {"type": "object",
                       "properties": {"city": {"type": "string"}},
                       "required": ["city"]},
    }},
    {"type": "function", "function": {
        "name": "convert_rate",
        "description": "FX",
        "parameters": {"type": "object",
                       "properties": {"amount": {"type": "string"},
                                      "from": {"type": "string"},
                                      "to": {"type": "string"}}},
    }},
]


def _fake_client(files: dict[str, str], writes: list[tuple[str, str]]):
    class FakeClient:
        async def read_text_file(self, path: str) -> str | None:
            return files.get(path)

        async def write_text_file(self, path: str, text: str) -> None:
            writes.append((path, text))
            files[path] = text

    return FakeClient()


def test_desired_content_appends_fenced_section() -> None:
    out = desired_content("IDENTITY.md", "# IDENTITY.md\n\n- **Name:** Muse\n")
    assert out.startswith("# IDENTITY.md")
    assert MANAGED_BEGIN in out and MANAGED_END in out
    assert "Gateway sessions" in out
    assert "- **Name:** Muse" in out


def test_desired_content_replaces_legacy_section() -> None:
    original = "# SOUL.md\n\nbase persona\n\n<!-- muse2api-managed: this section is maintained by muse2api -->\n\n## old\n- stale\n"
    out = desired_content("SOUL.md", original)
    assert "stale" not in out
    assert "base persona" in out
    assert "muse.memory_search" in out
    assert out.count(MANAGED_BEGIN) == 1


def test_desired_content_is_idempotent() -> None:
    once = desired_content("AGENTS.md", "# AGENTS.md\nmanual\n")
    twice = desired_content("AGENTS.md", once)
    assert once == twice


def test_harness_ensures_backs_up_and_writes(tmp_path: Path) -> None:
    files = {"IDENTITY.md": "# IDENTITY.md\noriginal\n"}
    writes: list[tuple[str, str]] = []
    policy = HarnessPolicy("acc1", tmp_path)
    changed = asyncio.run(policy.ensure(_fake_client(files, writes)))
    assert set(changed) == {"IDENTITY.md", "SOUL.md", "AGENTS.md"}
    backup = (tmp_path / "acc1" / "IDENTITY.md").read_text(encoding="utf-8")
    assert "original" in backup
    assert MANAGED_BEGIN in files["IDENTITY.md"]
    # second run converges without writes
    writes.clear()
    changed2 = asyncio.run(policy.ensure(_fake_client(files, writes)))
    assert changed2 == {} and writes == []


def test_harness_restore_puts_originals_back(tmp_path: Path) -> None:
    files = {"IDENTITY.md": "# IDENTITY.md\noriginal\n", "SOUL.md": "# SOUL.md\n"}
    writes: list[tuple[str, str]] = []
    policy = HarnessPolicy("acc1", tmp_path)
    asyncio.run(policy.ensure(_fake_client(files, writes)))
    restored = asyncio.run(policy.restore(_fake_client(files, writes)))
    assert restored == ["IDENTITY.md", "SOUL.md", "AGENTS.md"]
    assert files["IDENTITY.md"] == "# IDENTITY.md\noriginal\n"
    assert policy.status()["applied"] == []


def test_parse_tool_calls_list_native_multi() -> None:
    text = json.dumps({"calls": [
        {"tool": "get_weather", "arguments": {"city": "上海"}},
        {"tool": "convert_rate", "arguments": {"amount": "100", "from": "USD", "to": "CNY"}},
    ]}, ensure_ascii=False)
    calls = tool_plan.parse_tool_calls_list(text, TOOLS)
    assert calls is not None and len(calls) == 2
    assert calls[0][0] == "get_weather" and calls[0][1] == {"city": "上海"}
    assert calls[1][0] == "convert_rate"


def test_parse_tool_calls_list_single_and_openai_shapes() -> None:
    flat = json.dumps({"tool": "get_weather", "arguments": {"city": "北京"}}, ensure_ascii=False)
    assert tool_plan.parse_tool_calls_list(flat, TOOLS) == [("get_weather", {"city": "北京"})]
    openai_style = json.dumps({"tool_calls": [
        {"function": {"name": "get_weather", "arguments": "{\"city\":\"广州\"}"}},
    ]}, ensure_ascii=False)
    calls = tool_plan.parse_tool_calls_list(openai_style, TOOLS)
    assert calls == [("get_weather", {"city": "广州"})]
    # unknown tool -> not a tool reply
    bad = json.dumps({"calls": [{"tool": "nope", "arguments": {}}]})
    assert tool_plan.parse_tool_calls_list(bad, TOOLS) is None
    # integer argument coerced to the schema's string type
    coerced = json.dumps({"tool": "get_weather", "arguments": {"city": 123456}})
    assert tool_plan.parse_tool_calls_list(coerced, TOOLS) == [("get_weather", {"city": "123456"})]


@pytest.mark.anyio
async def test_completion_emits_parallel_tool_calls() -> None:
    """The buffered stream path rewrites multi-call JSON into N tool_calls chunks."""
    from muse2api.api.routes.chat import _completion, _Plan

    plan = _Plan(prompt="p", followup_prompt=None, turns=[], images=[],
                 images_current_only=True, system_prompt="", current_user_text="",
                 system_fp=None, tools=TOOLS)
    text = json.dumps({"calls": [
        {"tool": "get_weather", "arguments": {"city": "上海"}},
        {"tool": "convert_rate", "arguments": {"amount": "100", "from": "USD", "to": "CNY"}},
    ]}, ensure_ascii=False)
    body = _completion(plan, text, "chatcmpl-x", 1, "muse-chat")
    message = body["choices"][0]["message"]
    assert body["choices"][0]["finish_reason"] == "tool_calls"
    assert [c["function"]["name"] for c in message["tool_calls"]] == [
        "get_weather", "convert_rate"]
    ids = {c["id"] for c in message["tool_calls"]}
    assert len(ids) == 2
