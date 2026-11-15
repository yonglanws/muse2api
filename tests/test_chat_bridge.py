from __future__ import annotations

import json

from muse2api.core import system_prompt, tool_plan

TOOLS = [{
    "type": "function",
    "function": {
        "name": "get_group_member_info",
        "description": "Look up a group member",
        "parameters": {
            "type": "object",
            "properties": {
                "group_id": {"type": "string"},
                "user_id": {"type": "string"},
            },
            "required": ["group_id", "user_id"],
        },
    },
}]


def test_system_prompt_is_a_leading_instruction_block():
    plan = system_prompt.build_prompt([
        {"role": "system", "content": "Be concise."},
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi"},
        {"role": "user", "content": "What is the weather?"},
    ])
    assert plan.prompt.startswith("【系统指令｜最高优先级】\nBe concise.")
    assert "【当前用户消息】\nWhat is the weather?" in plan.prompt
    assert "Hello" in plan.prompt  # earlier turn stays as history
    assert plan.current_user_text == "What is the weather?"
    # The follow-up relies on the thread already holding the instructions.
    assert plan.followup_prompt == "【当前用户消息】\nWhat is the weather?\n\n" \
                                   "【任务】直接回复当前用户消息，不要重复或总结历史。只输出回复正文。"


def test_content_parts_and_images():
    plan = system_prompt.build_prompt([{
        "role": "user",
        "content": [
            {"type": "text", "text": "describe this"},
            {"type": "image_url", "image_url": {"url": "https://example.com/a.png"}},
        ],
    }])
    assert plan.current_user_text == "describe this\n[用户附带图片]"
    assert plan.images == ["https://example.com/a.png"]


def test_system_fingerprint_tracks_instruction_changes():
    a = system_prompt.build_prompt([
        {"role": "system", "content": "Be terse."},
        {"role": "user", "content": "hi"},
    ])
    b = system_prompt.build_prompt([
        {"role": "system", "content": "Be terse."},
        {"role": "user", "content": "hi"},
    ])
    c = system_prompt.build_prompt([
        {"role": "system", "content": "Be verbose."},
        {"role": "user", "content": "hi"},
    ])
    assert a.system_fingerprint == b.system_fingerprint
    assert a.system_fingerprint != c.system_fingerprint


def test_tool_continuation_is_detected_from_a_tool_message():
    messages = [
        {"role": "user", "content": "look it up"},
        {"role": "assistant", "content": None, "tool_calls": [{"function": {"name": "x"}}]},
        {"role": "tool", "content": "result"},
    ]
    assert system_prompt.is_tool_continuation(messages) is True
    assert system_prompt.is_tool_continuation([{"role": "user", "content": "hi"}]) is False


def test_tool_prompt_keeps_system_and_tool_schema():
    plan = system_prompt.build_tool_prompt(
        [{"role": "system", "content": "Be concise."},
         {"role": "user", "content": "find user 789"}],
        TOOLS,
    )
    assert "【系统指令｜最高优先级】" in plan.prompt
    assert "get_group_member_info" in plan.prompt
    assert '{"tool":"工具名","arguments":{...}}' in plan.prompt
    assert plan.system_fingerprint


def test_parse_flat_plan_and_none():
    tool, args, reply = tool_plan.parse_tool_plan(
        '{"tool":"get_group_member_info","arguments":{"group_id":"1","user_id":"2"}}', TOOLS)
    assert tool == "get_group_member_info"
    assert args == {"group_id": "1", "user_id": "2"}
    assert reply is None

    tool, args, reply = tool_plan.parse_tool_plan(
        '{"tool":"NONE","reply":"just chatting"}', TOOLS)
    assert tool is None and args is None and reply == "just chatting"
    assert tool_plan.parse_tool_plan("plain text", TOOLS) == (None, None, None)


def test_parse_tool_calls_wrapped_and_single():
    wrapped = json.dumps({"tool_calls": [{"name": "get_group_member_info",
                                          "arguments": {"group_id": "1", "user_id": 2}}]})
    calls = tool_plan.parse_tool_calls(wrapped, TOOLS)
    assert calls and calls[0]["function"]["name"] == "get_group_member_info"
    assert json.loads(calls[0]["function"]["arguments"]) == {"group_id": "1", "user_id": 2}
    assert tool_plan.parse_tool_calls("just a normal reply", TOOLS) is None


def test_arguments_are_coerced_to_the_schema():
    tool, args, _ = tool_plan.parse_tool_plan(
        '{"tool":"get_group_member_info","arguments":{"group_id":123,"user_id":789}}', TOOLS)
    assert args == {"group_id": "123", "user_id": "789"}


def test_conversation_key_uses_standard_user_only(client, auth, monkeypatch):
    gateway = client.app.state.services.gateway
    seen = []
    original = gateway.chat_stream

    async def spy(req):
        seen.append(req)
        async for delta in original(req):
            yield delta

    monkeypatch.setattr(gateway, "chat_stream", spy)
    body = {"model": "muse-chat", "messages": [{"role": "user", "content": "hi"}]}
    r = client.post("/v1/chat/completions", headers={
        **auth, "x-conversation-id": "conv-1"}, json=body)
    assert r.status_code == 200
    assert seen[0].conversation_hint is None

    body["user"] = "alice"
    r = client.post("/v1/chat/completions", headers=auth, json=body)
    assert r.status_code == 200
    assert seen[1].conversation_hint.endswith(":alice")


def test_planner_tool_call_returns_openai_tool_calls(planner_client, auth, monkeypatch):
    gateway = planner_client.app.state.services.gateway

    async def fake(_req):
        yield json.dumps({"tool": "get_group_member_info",
                          "arguments": {"group_id": "123456", "user_id": "789"}})

    monkeypatch.setattr(gateway, "chat_stream", fake)
    r = planner_client.post("/v1/chat/completions", headers=auth, json={
        "model": "muse-chat",
        "messages": [{"role": "user", "content": "find this member"}],
        "tools": TOOLS,
    })
    assert r.status_code == 200
    choice = r.json()["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    call = choice["message"]["tool_calls"][0]
    assert call["function"]["name"] == "get_group_member_info"
    assert json.loads(call["function"]["arguments"]) == {
        "group_id": "123456", "user_id": "789"}


def test_planner_none_returns_the_reply(planner_client, auth, monkeypatch):
    gateway = planner_client.app.state.services.gateway

    async def fake(_req):
        yield '{"tool":"NONE","reply":"hello there"}'

    monkeypatch.setattr(gateway, "chat_stream", fake)
    r = planner_client.post("/v1/chat/completions", headers=auth, json={
        "model": "muse-chat",
        "messages": [{"role": "user", "content": "just chatting"}],
        "tools": TOOLS,
    })
    assert r.status_code == 200
    assert r.json()["choices"][0]["message"]["content"] == "hello there"
    assert r.json()["choices"][0]["finish_reason"] == "stop"

def test_planner_refusal_is_retried_once(planner_client, auth, monkeypatch):
    gateway = planner_client.app.state.services.gateway
    calls = []

    async def fake(_req):
        calls.append(1)
        if len(calls) == 1:
            yield "我没有这个工具，也没有办法调用。"
        else:
            yield json.dumps({"tool": "get_group_member_info",
                              "arguments": {"group_id": "1", "user_id": "2"}})

    monkeypatch.setattr(gateway, "chat_stream", fake)
    r = planner_client.post("/v1/chat/completions", headers=auth, json={
        "model": "muse-chat",
        "messages": [{"role": "user", "content": "find this member"}],
        "tools": TOOLS,
    })
    assert r.status_code == 200
    assert len(calls) == 2
    assert r.json()["choices"][0]["finish_reason"] == "tool_calls"
