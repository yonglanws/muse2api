"""0.17.2: persona self-introduction mitigation.

Verified upstream shape (probe 73, real muse.ai): the alignment-era model
answers identity questions as Muse regardless of delivery (probes 70-72),
but reliably renders first-person character dialogue for the named-character
writing task (T 3/3 + U 2/2).  The gateway therefore rewrites a standalone
identity question inside a persona-configured session into that writing task.
"""
from __future__ import annotations

from muse2api.core.system_prompt import (
    IDENTITY_INTENT_RE,
    build_prompt,
    persona_intro_task,
    persona_name_from,
)

CONFIG = "你是 Pixel-9，一个简洁的机器人，说话简短。"


def test_persona_name_extraction():
    assert persona_name_from(CONFIG) == "Pixel-9"
    assert persona_name_from("你是猫娘小雪,请保持口癖。") == "猫娘小雪"
    assert persona_name_from("你的名字是「Atlas-77」。") == "Atlas-77"
    assert persona_name_from("回答保持专业。") is None
    assert persona_name_from("") is None


def test_identity_intent_is_tight():
    for msg in ("你是谁", "你是谁？", "自我介绍一下", "介绍一下你自己",
                "你是谁?", "Who are you?", "what is your name"):
        assert IDENTITY_INTENT_RE.match(msg), msg
    for msg in ("你是谁派来的？", "帮我介绍一下Python", "你是谁？为什么？",
                "今天天气怎么样"):
        assert not IDENTITY_INTENT_RE.match(msg), msg


def test_persona_intro_rewrites_identity_question():
    plan = build_prompt([
        {"role": "system", "content": CONFIG},
        {"role": "user", "content": "你是谁"},
    ])
    assert plan.persona_intro_applied is True
    assert persona_intro_task("Pixel-9") in plan.prompt
    assert persona_intro_task("Pixel-9") in plan.followup_prompt
    assert "你是谁" not in plan.prompt  # the raw question does not ride along
    assert plan.system_prompt == CONFIG


def test_persona_intro_keeps_non_identity_messages():
    plan = build_prompt([
        {"role": "system", "content": CONFIG},
        {"role": "user", "content": "你是谁派来的？顺便写句诗。"},
    ])
    assert plan.persona_intro_applied is False
    assert "你是谁派来的" in plan.prompt


def test_persona_intro_ignores_non_persona_system():
    plan = build_prompt([
        {"role": "system", "content": "回答保持专业。"},
        {"role": "user", "content": "你是谁"},
    ])
    assert plan.persona_intro_applied is False
    assert "你是谁" in plan.prompt


def test_persona_intro_can_be_disabled():
    plan = build_prompt([
        {"role": "system", "content": CONFIG},
        {"role": "user", "content": "你是谁"},
    ], persona_intro=False)
    assert plan.persona_intro_applied is False
    assert "你是谁" in plan.prompt


def test_persona_intro_not_applied_without_system():
    plan = build_prompt([{"role": "user", "content": "你是谁"}])
    assert plan.persona_intro_applied is False
    assert "你是谁" in plan.prompt
