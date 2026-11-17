"""0.18.0: web-chat read side — mark_seen parity, session history, timezone guard.

Recon basis (web bundle, 2026-11-17):
- the web client calls ``chat.mark_seen`` ({thread_id} in the path) whenever a
  transcript is on screen, so no thread keeps an unread badge; gateway turns
  are read by definition and must do the same;
- ``chat.history`` pages with ``{limit, after_seq}`` and answers with
  ``chat_events`` (each event carries ``seq``/``role``) — the read side the
  gateway lacked;
- the per-turn developer time-tag needs the request ``timezone``; because the
  value is sticky upstream, a bad IANA key must be rejected at startup.
"""
from __future__ import annotations

import pytest

from muse2api.accounts.model import Account
from muse2api.config import Settings
from test_protocol_driver import _chat, _driver_with_fake


# ------------------------------------------------------------------ mark_seen
async def test_completed_turn_marks_thread_seen():
    driver, client = _driver_with_fake()
    account = Account(id="acc_test")
    [text async for text in driver.chat_stream(
        account, _chat("hello", [("user", "hello")]))]
    session_id = client.calls[0]["session_id"]
    assert getattr(client, "seen_calls", []) == [session_id]


async def test_mark_seen_failure_is_tolerated():
    driver, client = _driver_with_fake()

    async def boom(thread_id: str) -> None:
        raise RuntimeError("mark_seen exploded")

    client.mark_seen = boom  # type: ignore[method-assign]

    account = Account(id="acc_test")
    chunks = [chunk async for chunk in driver.chat_stream(
        account, _chat("hello", [("user", "hello")]))]
    assert chunks and chunks[0].startswith("reply:")


# ------------------------------------------------------------- session history
async def test_session_history_passes_web_paging_params():
    driver, client = _driver_with_fake()
    seen: dict = {}

    async def chat_history(session_id: str, *, limit: int = 100,
                           after_seq: int | None = None,
                           before_seq: int | None = None) -> dict:
        seen.update({"session_id": session_id, "limit": limit,
                     "after_seq": after_seq, "before_seq": before_seq})
        return {"chat_events": [{"seq": 1, "role": "user"}]}

    client.chat_history = chat_history  # type: ignore[method-assign]
    account = Account(id="acc_test")
    result = await driver.session_history(
        account, "sess-1", limit=50, after_seq=10)
    assert result["chat_events"][0]["role"] == "user"
    assert seen == {"session_id": "sess-1", "limit": 50,
                    "after_seq": 10, "before_seq": None}


# ---------------------------------------------------------------- timezone guard
def test_timezone_accepts_iana_key():
    assert Settings(timezone="Asia/Shanghai").timezone == "Asia/Shanghai"
    assert Settings(timezone="UTC").timezone == "UTC"
    assert Settings(timezone="  ").timezone == ""


def test_timezone_rejects_typo():
    with pytest.raises(Exception) as excinfo:
        Settings(timezone="Asia/Shanghia")
    assert "IANA" in str(excinfo.value)
