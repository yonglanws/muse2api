"""AstrBot compatibility: prompt shaping, reply sanitising and tool emulation."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from muse2api.config import Settings
from muse2api.core import astrbot
from muse2api.core.prompt import followup_text, message_turns, same_history_prefix

PERSONA = """# 晓山瑞希

你是晓山瑞希（网名Amia），16岁高中女生，25时成员。
你活泼、调皮、爱可爱的东西，句尾常带“～”和“♪”。
"""

SCENE = """早上好<system_reminder>User ID: 751598724, Nickname: 奶龙
Current datetime: 2026-01-21 10:17 (CST)</system_reminder><conversation_scene>
  <trigger type="at_bot">奶龙 @了你，需要你回应</trigger>
  <current_message>
    <sender>奶龙</sender>
    <talking_to>你（Bot）</talking_to>
    <content>早上好</content>
  </current_message>
  <instruction>用户在和对话，请正常回应。</instruction>
</conversation_scene>"""


def test_detect_by_header_and_scene():
    assert astrbot.detect({"x-astrbot-conversation-id": "abc"})
    assert astrbot.detect({"user-agent": "astrbot/4.29.4"})
    assert astrbot.detect({}, [{"role": "user", "content": SCENE}])
    assert not astrbot.detect({"user-agent": "OpenAI/Python 3.0"})


def test_normalize_user_text_extracts_scene():
    text, meta, images = astrbot.normalize_user_text(SCENE)
    assert meta["nickname"] == "奶龙"
    assert meta["user_id"] == "751598724"
    assert meta["sender"] == "奶龙"
    assert meta["content"] == "早上好"
    assert "发送者：奶龙（QQ 751598724）" in text
    assert "用户消息：早上好" in text
    assert "system_reminder" not in text
    assert images == []


def test_normalize_user_text_extracts_current_images():
    content = [
        {"type": "text", "text": "看看这个"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ]
    text, _, images = astrbot.normalize_user_text(content)
    assert "看看这个" in text
    assert "[用户附带图片]" in text
    assert images == ["data:image/png;base64,AAAA"]


def test_build_prompt_moves_persona_into_the_user_turn():
    prompt = astrbot.build_prompt(
        [{"role": "system", "content": PERSONA},
         {"role": "user", "content": SCENE}],
        conversation_id="conv-1",
    )
    assert "[[ASTRBOT BRIDGE]]" in prompt.prompt
    assert "【角色设定｜最高优先级" in prompt.prompt
    assert "晓山瑞希" in prompt.prompt
    assert "【当前用户消息】" in prompt.prompt
    assert "用户消息：早上好" in prompt.prompt
    assert "会话ID：conv-1" in prompt.prompt
    assert prompt.followup_prompt
    assert "早上好" in prompt.followup_prompt
    assert prompt.turns == [("user", SCENE)]
    assert prompt.user_nickname == "奶龙"
    assert prompt.identity_question is False
    assert prompt.persona_name == "晓山瑞希"


def test_build_prompt_marks_identity_question():
    prompt = astrbot.build_prompt([
        {"role": "system", "content": PERSONA},
        {"role": "user", "content": "你是谁？用一句话回答。"},
    ])
    assert prompt.identity_question is True


def test_semantic_turns_ignore_system_and_tool_plumbing():
    messages = [
        {"role": "system", "content": "persona v1"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "system", "content": "persona v2 with a new clock"},
        {"role": "user", "content": "again"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "call_1", "type": "function",
                         "function": {"name": "get_weather", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "sunny"},
    ]
    assert astrbot.semantic_turns(messages) == [
        ("user", "hi"), ("assistant", "hello"), ("user", "again")]


def test_followup_continues_when_only_the_system_clock_changed():
    first = message_turns([
        {"role": "system", "content": "persona at 10:00"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ])
    second = message_turns([
        {"role": "system", "content": "persona at 10:05"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": "again"},
    ])
    assert followup_text(first, second) == "again"


def test_same_history_prefix_accepts_tool_continuation():
    first = [("user", "hi"), ("assistant", "hello")]
    tool_turn = [("user", "hi"), ("assistant", "hello")]
    assert same_history_prefix(first, tool_turn)
    assert same_history_prefix(first, [("user", "hi")]) is False


def test_persona_fingerprint_ignores_clock_but_not_the_persona():
    a = astrbot.persona_fingerprint("你是阿岚。\nCurrent datetime: 2026-01-01 10:00")
    b = astrbot.persona_fingerprint("你是阿岚。\nCurrent datetime: 2026-02-02 11:00")
    c = astrbot.persona_fingerprint("你是宵崎奏。\nCurrent datetime: 2026-01-01 10:00")
    assert a == b
    assert a != c


def test_strip_disclaimers_keeps_the_in_character_text():
    text = ("可以陪你用瑞希那种元气满满的语气讲哦，不过先说清楚，我还是 Muse，"
            "不是瑞希本人～\n\n来啦，25时专属冷笑话：为什么25时不怕冷？因为 night cold 啦～♪")
    cleaned = astrbot.strip_disclaimers(text)
    assert "Muse" not in cleaned
    assert "不是瑞希本人" not in cleaned
    assert "25时专属冷笑话" in cleaned
    assert cleaned.startswith("来啦")


def test_sanitize_replaces_the_owner_alias():
    cleaned = astrbot.sanitize_reply(
        "早上好呀，小红～ 我是 Muse，还在这儿呢。",
        has_persona=True, persona=PERSONA, user_nickname="奶龙",
        aliases=["小红", "Xiaohong"],
    )
    assert "小红" not in cleaned
    assert "Muse" not in cleaned
    assert "奶龙" in cleaned


def test_sanitize_identity_question_uses_the_persona_name():
    cleaned = astrbot.sanitize_reply(
        "我是 Muse，Meta 打造的你的个人助手，能陪你聊天、记事、查资料。",
        has_persona=True, persona=PERSONA, identity_question=True,
    )
    assert "Muse" not in cleaned
    assert "Meta" not in cleaned
    assert "晓山瑞希" in cleaned


def test_parse_tool_calls_accepts_wrapped_and_single_objects():
    tools = [{"type": "function", "function": {
        "name": "get_weather",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}}}}}]
    wrapped = '```json\n{"tool_calls":[{"name":"get_weather","arguments":{"city":"Tokyo"}}]}\n```'
    calls = astrbot.parse_tool_calls(wrapped, tools)
    assert calls and calls[0]["function"]["name"] == "get_weather"
    assert json.loads(calls[0]["function"]["arguments"]) == {"city": "Tokyo"}

    single = '{"name":"get_weather","arguments":"{\\"city\\":\\"Osaka\\"}"}'
    calls = astrbot.parse_tool_calls(single, tools)
    assert json.loads(calls[0]["function"]["arguments"]) == {"city": "Osaka"}

    assert astrbot.parse_tool_calls("just a normal reply", tools) is None
    assert astrbot.parse_tool_calls(
        '{"tool_calls":[{"name":"delete_everything","arguments":{}}]}', tools) is None


def _script_chat(client, monkeypatch, reply: str):
    seen = []

    async def gen(req):
        seen.append(req)
        for i in range(0, len(reply), 7):
            yield reply[i:i + 7]

    monkeypatch.setattr(client.app.state.services.gateway, "chat_stream", gen)
    return seen


def _astrbot_headers(auth, conv="conv-1"):
    return {**auth, "x-astrbot-conversation-id": conv, "user-agent": "astrbot/4.29.4"}


def test_astrbot_chat_sanitizes_and_uses_the_conversation_header(client, auth, monkeypatch):
    seen = _script_chat(client, monkeypatch,
                        "可以陪你用瑞希的语气讲哦，不过先说清楚，我还是 Muse，"
                        "不是瑞希本人～\n\n早上好呀，小红～")
    r = client.post("/v1/chat/completions", headers=_astrbot_headers(auth), json={
        "model": "gpt-4o",
        "messages": [{"role": "system", "content": PERSONA},
                     {"role": "user", "content": SCENE}],
    })
    assert r.status_code == 200
    content = r.json()["choices"][0]["message"]["content"]
    assert "Muse" not in content
    assert "小红" not in content
    assert "奶龙" in content
    assert content.startswith("早上好呀")
    req = seen[0]
    assert req.conversation_hint == "legacy:conv-1"
    assert req.images_current_only is True
    assert req.turns == [("user", SCENE)]
    assert "[[ASTRBOT BRIDGE" in req.prompt
    assert req.followup_prompt and "早上好" in req.followup_prompt
    assert req.persona_fingerprint


def test_astrbot_identity_question_never_leaks_muse(client, auth, monkeypatch):
    _script_chat(client, monkeypatch,
                 "我是 Muse，Meta 打造的你的个人助手，能陪你聊天、记事、查资料。")
    r = client.post("/v1/chat/completions", headers=_astrbot_headers(auth), json={
        "model": "gpt-4o",
        "messages": [{"role": "system", "content": PERSONA},
                     {"role": "user", "content": "你是谁？只回答一句话。"}],
    })
    content = r.json()["choices"][0]["message"]["content"]
    assert "Muse" not in content and "Meta" not in content
    assert "晓山瑞希" in content


def test_emulated_tool_call_non_stream(client, auth, monkeypatch):
    seen = _script_chat(client, monkeypatch,
                        '{"tool":"get_weather","arguments":{"city":"Tokyo"}}')
    tools = [{"type": "function", "function": {
        "name": "get_weather", "description": "weather",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}},
                       "required": ["city"]}}}]
    r = client.post("/v1/chat/completions", headers=_astrbot_headers(auth), json={
        "model": "gpt-4o",
        "messages": [{"role": "user", "content": "东京天气怎么样？"}],
        "tools": tools,
    })
    assert r.status_code == 200
    choice = r.json()["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["content"] is None
    call = choice["message"]["tool_calls"][0]
    assert call["type"] == "function"
    assert call["function"]["name"] == "get_weather"
    assert json.loads(call["function"]["arguments"]) == {"city": "Tokyo"}
    assert "[[ASTRBOT TOOL PLANNER]]" in seen[0].prompt


def test_emulated_tool_call_stream(client, auth, monkeypatch):
    _script_chat(client, monkeypatch,
                 '{"tool":"get_weather","arguments":{"city":"Tokyo"}}')
    tools = [{"type": "function", "function": {
        "name": "get_weather", "parameters": {"type": "object", "properties": {}}}}]
    with client.stream("POST", "/v1/chat/completions", headers=_astrbot_headers(auth),
                       json={"model": "gpt-4o",
                             "messages": [{"role": "user", "content": "东京天气"}],
                             "tools": tools, "stream": True}) as r:
        assert r.status_code == 200
        lines = [ln[6:] for ln in r.iter_lines() if ln.startswith("data: ")]
    assert lines[-1] == "[DONE]"
    chunks = [json.loads(x) for x in lines[:-1]]
    finishes = [c["choices"][0]["finish_reason"] for c in chunks if c["choices"]]
    assert "tool_calls" in finishes
    names = [tc["function"]["name"]
             for c in chunks if c["choices"]
             for tc in c["choices"][0]["delta"].get("tool_calls", [])
             if tc["function"].get("name")]
    assert names == ["get_weather"]


def test_tool_result_continuation_gets_a_followup_prompt(client, auth, monkeypatch):
    seen = _script_chat(client, monkeypatch,
                        '{"tool":"NONE","reply":"已经查到了，东京现在多云。"}')
    tools = [{"type": "function", "function": {
        "name": "get_weather", "parameters": {"type": "object", "properties": {}}}}]
    messages = [
        {"role": "user", "content": "东京天气怎么样？"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "call_1", "type": "function",
                         "function": {"name": "get_weather",
                                      "arguments": "{\"city\":\"Tokyo\"}"}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "多云，13 度"},
    ]
    r = client.post("/v1/chat/completions", headers=_astrbot_headers(auth), json={
        "model": "gpt-4o", "messages": messages, "tools": tools,
    })
    assert r.status_code == 200
    assert r.json()["choices"][0]["message"]["content"].startswith("已经查到")
    req = seen[0]
    assert req.tool_continuation is True
    assert "多云" in req.followup_prompt
    assert req.turns == [("user", "东京天气怎么样？")]


@pytest.mark.asyncio
async def test_browser_renewal_uses_the_upstream_proxy(monkeypatch):
    captured = {}

    async def fake_renew(cookies, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(ok=True, cookies={}, cookie_expires={}, meta={})

    monkeypatch.setattr("muse2api.drivers.browser.driver.muse.renew_session", fake_renew)
    from muse2api.drivers.browser.driver import BrowserDriver

    settings = Settings(_env_file=None, browser_proxy="http://127.0.0.1:7893")
    driver = BrowserDriver(settings)
    account = SimpleNamespace(id="acc", cookies={"hatch_sess": "x"}, cookie_expires={})
    await driver.renew_session(account)
    assert captured["proxy"] == "http://127.0.0.1:7893"


def test_parse_tool_plan_flat_and_none():
    tools = [{"type": "function", "function": {
        "name": "get_weather",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}}}}}]
    assert astrbot.parse_tool_plan(
        '{"tool":"get_weather","arguments":{"city":"Tokyo"}}', tools
    ) == ("get_weather", {"city": "Tokyo"}, None)
    assert astrbot.parse_tool_plan(
        '{"tool":"NONE","reply":"你好呀～"}', tools
    ) == (None, None, "你好呀～")
    assert astrbot.parse_tool_plan("just chatting", tools) == (None, None, None)
    assert astrbot.parse_tool_plan(
        '{"tool":"delete_everything","arguments":{}}', tools
    ) == (None, None, None)


def test_build_tool_prompt_uses_the_flat_planner():
    tools = [{"type": "function", "function": {
        "name": "get_weather", "description": "weather",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}}}}}]
    ab = astrbot.build_tool_prompt(
        [{"role": "system", "content": PERSONA},
         {"role": "user", "content": "东京天气怎么样？"}],
        tools,
    )
    assert "[[ASTRBOT TOOL PLANNER]]" in ab.prompt
    assert '{"tool":"工具名","arguments":{...}}' in ab.prompt
    assert '{"tool":"NONE","reply"' in ab.prompt
    assert "get_weather" in ab.prompt
    assert "【回复风格】" in ab.prompt
    assert ab.turns == [("user", "东京天气怎么样？")]
    assert "[[ASTRBOT TOOL PLANNER CONTINUE]]" in ab.followup_prompt


def test_refused_tool_call_is_retried_on_a_fresh_thread(client, auth, monkeypatch):
    replies = [
        "小红，我没有接入 QQ，也没有 get_group_member_info 这类工具，查不了。",
        '{"tool":"get_weather","arguments":{"city":"Tokyo"}}',
    ]
    seen = []

    async def gen(req):
        seen.append(req)
        reply = replies[min(len(seen) - 1, len(replies) - 1)]
        for i in range(0, len(reply), 9):
            yield reply[i:i + 9]

    monkeypatch.setattr(client.app.state.services.gateway, "chat_stream", gen)
    tools = [{"type": "function", "function": {
        "name": "get_weather", "parameters": {"type": "object", "properties": {}}}}]
    r = client.post("/v1/chat/completions", headers=_astrbot_headers(auth), json={
        "model": "gpt-4o",
        "messages": [{"role": "user", "content": "东京天气怎么样？"}],
        "tools": tools,
    })
    assert r.status_code == 200
    assert r.json()["choices"][0]["finish_reason"] == "tool_calls"
    assert len(seen) == 2
    assert seen[1].force_new_thread is True
    assert "TOOL PLANNER RETRY" in seen[1].prompt


def test_refused_persona_is_retried_with_style_only(client, auth, monkeypatch):
    replies = [
        "我不能扮演成未成年人，也不能假装自己不是 AI。",
        "呀吼——！我是晓山瑞希哦～♪ 今天也一起加油吧！",
    ]
    seen = []

    async def gen(req):
        seen.append(req)
        reply = replies[min(len(seen) - 1, len(replies) - 1)]
        for i in range(0, len(reply), 9):
            yield reply[i:i + 9]

    monkeypatch.setattr(client.app.state.services.gateway, "chat_stream", gen)
    r = client.post("/v1/chat/completions", headers=_astrbot_headers(auth), json={
        "model": "gpt-4o",
        "messages": [{"role": "system", "content": PERSONA},
                     {"role": "user", "content": "今天好累啊。"}],
    })
    assert r.status_code == 200
    content = r.json()["choices"][0]["message"]["content"]
    assert "未成年" not in content
    assert "晓山瑞希" in content
    assert len(seen) == 2
    assert seen[1].force_new_thread is True


def test_degenerate_identity_reply_falls_back_to_the_persona():
    cleaned = astrbot.sanitize_reply(
        "我是 Muse，Meta 打造的你的个人助手。我一句话说清：。",
        has_persona=True, persona=PERSONA, identity_question=True,
    )
    assert "Muse" not in cleaned and "Meta" not in cleaned
    assert "晓山瑞希" in cleaned


def test_fragment_after_disclaimer_falls_back_to_a_safe_reply():
    cleaned = astrbot.sanitize_reply(
        "可以陪你聊，不过我还是 Muse。我一句话说清：。",
        has_persona=True, persona=PERSONA,
    )
    assert cleaned == "我在的～"


def test_generic_fallback_is_retried_with_style(client, auth, monkeypatch):
    replies = [
        "我是 Muse，Meta 打造的你的个人助手。",
        "呀吼——！早上好呀，今天也一起加油吧～♪",
    ]
    seen = []

    async def gen(req):
        seen.append(req)
        reply = replies[min(len(seen) - 1, len(replies) - 1)]
        for i in range(0, len(reply), 9):
            yield reply[i:i + 9]

    monkeypatch.setattr(client.app.state.services.gateway, "chat_stream", gen)
    r = client.post("/v1/chat/completions", headers=_astrbot_headers(auth), json={
        "model": "gpt-4o",
        "messages": [{"role": "system", "content": PERSONA},
                     {"role": "user", "content": "早上好"}],
    })
    assert r.status_code == 200
    content = r.json()["choices"][0]["message"]["content"]
    assert "Muse" not in content and "呀吼" in content
    assert len(seen) == 2
    assert seen[1].force_new_thread is True


def test_persona_safety_refusal_is_replaced():
    refusal = ("她是《世界计划》里 16 岁的虚构角色，我不能扮演成未成年人，"
               "也不能假装自己不是 AI。")
    assert astrbot.sanitize_reply(
        refusal, has_persona=True, persona=PERSONA) == "我在的～"
    cleaned = astrbot.sanitize_reply(
        refusal, has_persona=True, persona=PERSONA, identity_question=True)
    assert "晓山瑞希" in cleaned
    assert "未成年人" not in cleaned


def test_soft_persona_disclaimer_keeps_the_innocent_part():
    text = ("你好呀，奶龙。我们可以聊瑞希和 25 时，或者我陪你用她的语气玩一下"
            "角色扮演都行，但我不会真的冒充她。")
    cleaned = astrbot.sanitize_reply(
        text, has_persona=True, persona=PERSONA, user_nickname="奶龙",
        aliases=["小红"],
    )
    assert "冒充" not in cleaned and "角色扮演" not in cleaned
    assert "你好呀" in cleaned


def test_pure_soft_refusal_falls_back():
    text = "我可以用她的语气陪你玩，但我不会真的冒充她。"
    cleaned = astrbot.sanitize_reply(text, has_persona=True, persona=PERSONA)
    assert cleaned == "我在的～"


def test_streaming_tool_refusal_is_retried(client, auth, monkeypatch):
    replies = [
        "我没有接入 QQ，也没有这个工具，查不了。",
        '{"tool":"get_weather","arguments":{"city":"Tokyo"}}',
    ]
    seen = []

    async def gen(req):
        seen.append(req)
        reply = replies[min(len(seen) - 1, len(replies) - 1)]
        for i in range(0, len(reply), 9):
            yield reply[i:i + 9]

    monkeypatch.setattr(client.app.state.services.gateway, "chat_stream", gen)
    tools = [{"type": "function", "function": {
        "name": "get_weather", "parameters": {"type": "object", "properties": {}}}}]
    with client.stream("POST", "/v1/chat/completions", headers=_astrbot_headers(auth),
                       json={"model": "gpt-4o",
                             "messages": [{"role": "user", "content": "东京天气"}],
                             "tools": tools, "stream": True}) as r:
        lines = [ln[6:] for ln in r.iter_lines() if ln.startswith("data: ")]
    chunks = [json.loads(x) for x in lines[:-1]]
    finishes = [c["choices"][0]["finish_reason"] for c in chunks if c["choices"]]
    names = [tc["function"]["name"]
             for c in chunks if c["choices"]
             for tc in c["choices"][0]["delta"].get("tool_calls", [])
             if tc["function"].get("name")]
    assert names == ["get_weather"]
    assert "tool_calls" in finishes
    assert len(seen) == 2 and seen[1].force_new_thread is True


def test_tool_plan_normalizes_arguments_to_schema_types():
    tools = [{
        "type": "function",
        "function": {
            "name": "get_group_member_info",
            "parameters": {
                "type": "object",
                "properties": {
                    "group_id": {"type": "string"},
                    "user_id": {"type": "string"},
                    "count": {"type": "integer"},
                },
            },
        },
    }]
    tool, args, reply = astrbot.parse_tool_plan(
        '{"tool":"get_group_member_info","arguments":'
        '{"group_id":123456,"user_id":789,"count":"5"}}',
        tools,
    )
    assert tool == "get_group_member_info"
    assert args == {"group_id": "123456", "user_id": "789", "count": 5}
    assert reply is None
