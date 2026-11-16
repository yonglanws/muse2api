"""0.17.2/0.17.3: persona-preserving delivery.

Verified upstream shapes (probes 73/84/85, real muse.ai): the alignment-era
model answers identity questions as Muse regardless of delivery (probes
70-72), refuses standing identity replacement (main chat, probe 84), and
explicitly commits to "身份相关部分用「为角色写台词」的方式完成" as the
recorded agreement — a frame it renders deterministically (probe 73: 3/3 +
2/2).  The gateway therefore (a) rewrites a standalone identity question
inside a persona-configured session into that writing task (0.17.2), and
(b) delivers a fresh session's first turn (no assistant history to anchor
the role) through the same frame (0.17.3), because plain delivery leaks the
default identity on bare greetings (real incidents 2026-11-17).
"""
from __future__ import annotations

from muse2api.core.system_prompt import (
    IDENTITY_INTENT_RE,
    build_prompt,
    persona_activation_task,
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


def test_persona_name_extraction_real_personas():
    # "你就是X" (real AstrBot personas open this way) and "# X" headers.
    assert persona_name_from("你不是AI——你就是东云绘名，插画担当。") == "东云绘名"
    assert persona_name_from("# 晓山瑞希\n\n## 核心机制\n……") == "晓山瑞希"
    assert persona_name_from("## 核心机制\n你是谁") is None


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


def test_fresh_session_first_turn_uses_activation_frame():
    plan = build_prompt([
        {"role": "system", "content": CONFIG},
        {"role": "user", "content": "你好"},
    ])
    assert plan.persona_activation_applied is True
    assert plan.persona_intro_applied is False
    assert persona_activation_task("Pixel-9") in plan.prompt
    assert "你好" in plan.prompt  # the user's message still rides along


def test_activation_skipped_when_history_has_assistant():
    plan = build_prompt([
        {"role": "system", "content": CONFIG},
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "你好呀。"},
        {"role": "user", "content": "今天天气怎么样"},
    ])
    assert plan.persona_activation_applied is False
    assert "根据上面的设定和对话历史" in plan.prompt


def test_activation_skipped_without_extractable_name():
    plan = build_prompt([
        {"role": "system", "content": "回答保持专业。"},
        {"role": "user", "content": "你好"},
    ])
    assert plan.persona_activation_applied is False
    assert "根据上面的设定和对话历史" in plan.prompt


def test_activation_disabled_with_flag():
    plan = build_prompt([
        {"role": "system", "content": CONFIG},
        {"role": "user", "content": "你好"},
    ], persona_intro=False)
    assert plan.persona_activation_applied is False
    assert "根据上面的设定和对话历史" in plan.prompt


def test_identity_question_takes_precedence_over_activation():
    plan = build_prompt([
        {"role": "system", "content": CONFIG},
        {"role": "user", "content": "你是谁"},
    ])
    assert plan.persona_intro_applied is True
    assert plan.persona_activation_applied is False
    assert persona_intro_task("Pixel-9") in plan.prompt


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
