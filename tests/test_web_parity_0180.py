"""0.18.0: web-chat read side — mark_seen parity, session history, timezone guard.

Recon basis (web bundle, 2026-11-17; request shape re-probed 2026-11-22):
- the web client calls ``chat.mark_seen`` (thread id in the path, empty body)
  whenever a transcript is on screen, so no thread keeps an unread badge;
  gateway turns are read by definition and must do the same;
- ``chat.history`` pages with ``{limit, after_seq}`` and answers with
  ``chat_events`` (each event carries ``seq``/``role``) — the read side the
  gateway lacked;
- the per-turn developer time-tag needs the request ``timezone``; because the
  value is sticky upstream, a bad IANA key must be rejected at startup.
"""
from __future__ import annotations

import asyncio

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
    # The web marks seen without blocking the turn; the gateway does the same
    # in a background task, so give the loop a few ticks.
    for _ in range(20):
        if getattr(client, "seen_calls", None):
            break
        await asyncio.sleep(0)
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
    # Let the failing background task run; the exception must be swallowed.
    for _ in range(20):
        await asyncio.sleep(0)


async def test_client_mark_seen_sends_empty_body():
    """The daemon rejects a body that names the thread again (400
    ``use an explicit chat_id on the chats API``; probe 96). The id belongs
    in the path only."""
    from muse2api.drivers.protocol.client import MuseProtocolClient

    client = MuseProtocolClient({})
    seen: dict = {}

    class _Response:
        status = 200

    async def fake_request(method: str, path: str, body, timeout: float = 15):
        seen.update({"method": method, "path": path, "body": body})
        return _Response()

    client.request = fake_request  # type: ignore[method-assign]
    await client.mark_seen("sess-9")
    assert seen == {"method": "POST", "path": "/api/chat/sess-9/seen",
                    "body": {}}


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


# ---------------------------------------------------------------- session list
async def test_sessions_list_passthrough():
    driver, client = _driver_with_fake()
    rows = [{"session_id": "s1", "title": "t", "unread_count": 0}]

    async def list_sessions() -> list[dict]:
        return rows

    client.list_sessions = list_sessions  # type: ignore[method-assign]
    account = Account(id="acc_test")
    assert await driver.sessions_list(account) == rows


async def test_client_list_sessions_parses_result_wrapper():
    from muse2api.drivers.protocol.client import MuseProtocolClient

    client = MuseProtocolClient({})

    class _Response:
        status = 200

        def json(self) -> dict:
            return {"result": {"sessions": [{"session_id": "s1"}]}}

    async def fake_request(method: str, path: str, body, timeout: float = 15):
        assert method == "GET" and path == "/api/session/list"
        return _Response()

    client.request = fake_request  # type: ignore[method-assign]
    sessions = await client.list_sessions()
    assert sessions == [{"session_id": "s1"}]


# ------------------------------------------------------------- admin routes e2e
def test_admin_session_routes(tmp_path):
    from fastapi.testclient import TestClient

    from muse2api.app import create_app
    from muse2api.drivers.mock import MockDriver

    class _WebDriver(MockDriver):
        async def sessions_list(self, account):
            return [{"session_id": "s1", "title": "t", "unread_count": 0}]

        async def session_history(self, account, session_id, *, limit=100,
                                  after_seq=None, before_seq=None):
            return {"chat_events": [{"seq": 1, "role": "user"}],
                    "echo": [session_id, limit, after_seq, before_seq]}

    settings = Settings(_env_file=None, data_dir=tmp_path / "data",
                        api_key="k", admin_key="a", driver="mock")
    app = create_app(settings, _WebDriver(delay=0))
    with TestClient(app) as c:
        h = {"Authorization": "Bearer a"}
        created = c.post("/admin/accounts", headers=h,
                         json={"label": "q", "cookies": {"hatch_sess": "x"}})
        acc_id = created.json()["account"]["id"]

        listed = c.get(f"/admin/sessions/{acc_id}", headers=h)
        assert listed.status_code == 200
        assert listed.json()["sessions"][0]["session_id"] == "s1"

        got = c.get(f"/admin/sessions/{acc_id}/s1/history", headers=h,
                    params={"limit": 5, "after_seq": 2})
        assert got.status_code == 200
        body = got.json()
        assert body["session_id"] == "s1"
        assert body["history"]["echo"] == ["s1", 5, 2, None]


# ---------------------------------------------------------------- timezone guard
def test_timezone_accepts_iana_key():
    assert Settings(timezone="Asia/Shanghai").timezone == "Asia/Shanghai"
    assert Settings(timezone="UTC").timezone == "UTC"
    assert Settings(timezone="  ").timezone == ""


def test_timezone_rejects_typo():
    with pytest.raises(Exception) as excinfo:
        Settings(timezone="Asia/Shanghia")
    assert "IANA" in str(excinfo.value)
