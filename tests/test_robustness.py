"""0.9.0 robustness and API-parity tests.

Driver: aborted turns are cancelled upstream and their thread discarded (the
aborted turn's events would otherwise be replayed into the next one), dead
subscriptions fail fast instead of spinning until the full timeout, and a
subscription closed between turns is reopened.

API: stop sequences truncate, JSON mode extracts and validates, n>1 returns
several choices (ephemeral threads), the envelope carries system_fingerprint,
errors always use the OpenAI envelope, and /v1/* answers CORS preflights.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from muse2api.accounts import Account, AccountPool, JsonAccountStore
from muse2api.app import create_app
from muse2api.config import Settings
from muse2api.drivers.base import ChatRequest, DriverCapabilities, MuseDriver
from muse2api.drivers.protocol.driver import ProtocolDriver
from muse2api.errors import UpstreamError
from muse2api.services.gateway import Gateway
from test_protocol_driver import FakeClient, _chat, _driver_with_fake

API_KEY = "test-key"


# --------------------------------------------------------------- driver: cancel

async def test_client_disconnect_cancels_the_upstream_turn():
    driver, client = _driver_with_fake()
    account = Account(id="acc_test")
    gen = driver.chat_stream(account, _chat("hello", [("user", "hello")]))
    assert await anext(gen)
    await gen.aclose()  # the client went away mid-stream
    assert client.cancelled is True
    assert "conv-1" not in driver._state(account.id).threads


async def test_aborted_turn_discards_the_thread():
    """A timeout leaves the upstream turn running; the thread must not be
    reused, or the orphan turn's events get replayed into the next one."""
    driver, client = _driver_with_fake()

    class DyingSub:
        closed = False
        events: list[dict] = []

        async def wait_from(self, index: int, timeout: float = 0.5):
            raise UpstreamError("Muse Noise connection closed")

        def close(self) -> None:
            pass

    async def subscribe(session_id, *, after_stream_seq: int = 0, **kwargs):
        client.sub_calls.append((session_id, after_stream_seq))
        return DyingSub()

    client.subscribe = subscribe  # type: ignore[method-assign]
    account = Account(id="acc_test")
    with pytest.raises(UpstreamError):
        _ = [d async for d in driver.chat_stream(
            account, _chat("hello", [("user", "hello")]))]
    assert client.cancelled is True
    assert "conv-1" not in driver._state(account.id).threads


async def test_dead_subscription_fails_fast():
    """A closed subscription must not spin until the full chat timeout."""
    driver, client = _driver_with_fake()

    class DeadSub:
        closed = True
        events: list[dict] = []

        async def wait_from(self, index: int, timeout: float = 0.5):
            await asyncio.sleep(0)
            return []

        def close(self) -> None:
            pass

    async def subscribe(session_id, *, after_stream_seq: int = 0, **kwargs):
        return DeadSub()

    client.subscribe = subscribe  # type: ignore[method-assign]
    account = Account(id="acc_test")
    req = _chat("hello", [("user", "hello")])
    req.timeout = 60  # would spin for a minute without the check
    with pytest.raises(UpstreamError, match="subscription"):
        await asyncio.wait_for(
            _collect(driver.chat_stream(account, req)), timeout=5)


async def test_dead_transport_fails_fast_via_alive_check():
    driver, client = _driver_with_fake()

    class OpenButDeadSub:
        closed = False  # reader still "running"
        events: list[dict] = []

        async def wait_from(self, index: int, timeout: float = 0.5):
            await asyncio.sleep(0)
            return []

        def close(self) -> None:
            pass

    async def subscribe(session_id, *, after_stream_seq: int = 0, **kwargs):
        return OpenButDeadSub()

    client.subscribe = subscribe  # type: ignore[method-assign]
    client.alive = False  # receiver task died
    account = Account(id="acc_test")
    with pytest.raises(UpstreamError, match="subscription"):
        await asyncio.wait_for(
            _collect(driver.chat_stream(
                account, _chat("hello", [("user", "hello")]))),
            timeout=5)


async def test_interrupted_status_is_a_normal_end():
    """The web stop button and our own /chat/cancel end with "interrupted";
    the partial text is the answer, not a failure."""

    class InterruptedClient(FakeClient):
        async def chat_stream(self, session_id, items, **kwargs):
            ack = await super().chat_stream(session_id, items, **kwargs)
            for ev in reversed(self._events):
                if ev.get("event") == "delta.message_done":
                    ev["payload"]["status"] = "interrupted"
                    break
            return ack

    driver = _driver_with_client(InterruptedClient())
    account = Account(id="acc_test")
    text = await _collect(driver.chat_stream(
        account, _chat("hello", [("user", "hello")])))
    assert "hello" in text
    assert not driver._fake_conn.client.cancelled
    # Completed (even interrupted) turns keep their thread.
    assert "conv-1" in driver._state(account.id).threads


async def test_failed_status_cancels_and_discards():
    class FailedClient(FakeClient):
        async def chat_stream(self, session_id, items, **kwargs):
            ack = await super().chat_stream(session_id, items, **kwargs)
            for ev in reversed(self._events):
                if ev.get("event") == "delta.message_done":
                    ev["payload"]["status"] = "failed"
                    break
            return ack

    driver = _driver_with_client(FailedClient())
    account = Account(id="acc_test")
    with pytest.raises(UpstreamError, match="failed"):
        _ = [d async for d in driver.chat_stream(
            account, _chat("hello", [("user", "hello")]))]
    assert driver._fake_conn.client.cancelled is True
    assert "conv-1" not in driver._state(account.id).threads


async def test_subscription_closed_between_turns_is_reopened():
    driver, client = _driver_with_fake()
    account = Account(id="acc_test")
    req = _chat("hello", [("user", "hello")])
    await _collect(driver.chat_stream(account, req))
    state = driver._state(account.id)
    thread = state.threads["conv-1"]
    assert thread.subscription is not None
    thread.subscription.close()

    req2 = _chat("more", [("user", "hello"), ("assistant", "reply:hello"),
                          ("user", "more")])
    await _collect(driver.chat_stream(account, req2))
    assert len(client.sub_calls) == 2
    # The reopened subscription resumes after the first turn's seq.
    assert client.sub_calls[1][1] == client.sub_calls[0][1] + 4


def _driver_with_client(client: FakeClient):
    from muse2api.drivers.protocol.driver import _Conn
    driver = ProtocolDriver(Settings())
    driver._fake_conn = _Conn(client=client)

    async def fake_acquire_conn(account: Account, state, preferred):
        return driver._fake_conn

    driver._acquire_conn = fake_acquire_conn  # type: ignore[method-assign]
    return driver


async def _collect(stream) -> str:
    return "".join([delta async for delta in stream])


# ---------------------------------------------------------------- gateway

class _FlagDriver(MuseDriver):
    name = "flag"
    requires_account = False
    capabilities = DriverCapabilities(chat=True)

    def __init__(self) -> None:
        self.closed_cleanly = False
        self.cancel_requested = False

    async def chat_stream(self, account, req):
        try:
            for piece in ("a", "b", "c"):
                await asyncio.sleep(0)
                yield piece
        finally:
            self.closed_cleanly = True
            self.cancel_requested = bool(req.cancel and req.cancel.is_set())


async def test_gateway_closes_the_driver_generator_immediately(tmp_path):
    pool = AccountPool(JsonAccountStore(tmp_path / "acc.json"))
    driver = _FlagDriver()
    gw = Gateway(pool, driver)
    gen = gw.chat_stream(ChatRequest(prompt="hi", model="m"))
    assert await anext(gen) == "a"
    await gen.aclose()  # client disconnect
    # Not at GC time: the driver cleanup must run inside the aclose() call.
    assert driver.closed_cleanly is True


# ---------------------------------------------------------------- API surface

class StubDriver(MuseDriver):
    name = "stub"
    requires_account = False
    capabilities = DriverCapabilities(chat=True)

    def __init__(self, reply: str, *, boom: bool = False) -> None:
        self.reply = reply
        self.boom = boom
        self.requests: list[ChatRequest] = []

    async def chat_stream(self, account, req):
        if self.boom:
            raise RuntimeError("stub explosion")
        self.requests.append(req)
        for i in range(0, len(self.reply), 4):
            await asyncio.sleep(0)
            yield self.reply[i:i + 4]


def _stub_client(tmp_path, driver, *, raise_server_exceptions: bool = True,
                 **settings_kw) -> TestClient:
    settings = Settings(
        _env_file=None, data_dir=tmp_path / "data", api_key=API_KEY,
        admin_key=API_KEY, driver="mock", **settings_kw)
    app = create_app(settings, driver)
    return TestClient(app, raise_server_exceptions=raise_server_exceptions)


def _auth() -> dict:
    return {"Authorization": f"Bearer {API_KEY}"}


def test_stop_truncates_non_stream_reply(tmp_path):
    driver = StubDriver("AAA-BBB-CCC")
    client = _stub_client(tmp_path, driver)
    r = client.post("/v1/chat/completions", headers=_auth(), json={
        "messages": [{"role": "user", "content": "hi"}], "stop": ["-BBB"]})
    assert r.status_code == 200
    assert r.json()["choices"][0]["message"]["content"] == "AAA"


def test_stop_truncates_stream_reply(tmp_path):
    driver = StubDriver("AAA-BBB-CCC")
    client = _stub_client(tmp_path, driver)
    with client.stream("POST", "/v1/chat/completions", headers=_auth(), json={
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True, "stop": ["-"]}) as r:
        lines = [ln[6:] for ln in r.iter_lines() if ln.startswith("data: ")]
    chunks = [json.loads(x) for x in lines if x != "[DONE]"]
    text = "".join(c["choices"][0]["delta"].get("content", "")
                   for c in chunks if c["choices"])
    assert text == "AAA"
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"


def test_json_mode_extracts_and_validates(tmp_path):
    driver = StubDriver('Sure! Here it is:\n```json\n{"city": "北京"}\n```')
    client = _stub_client(tmp_path, driver)
    r = client.post("/v1/chat/completions", headers=_auth(), json={
        "messages": [{"role": "user", "content": "weather?"}],
        "response_format": {"type": "json_object"}})
    assert r.status_code == 200
    value = json.loads(r.json()["choices"][0]["message"]["content"])
    assert value == {"city": "北京"}


def test_json_mode_retries_then_falls_back(tmp_path):
    driver = StubDriver("Sorry, I cannot do that.")
    client = _stub_client(tmp_path, driver)
    r = client.post("/v1/chat/completions", headers=_auth(), json={
        "messages": [{"role": "user", "content": "weather?"}],
        "response_format": {"type": "json_object"}})
    assert r.status_code == 200
    # best effort: the raw text comes back, retries bounded by tool_retries
    assert "Sorry" in r.json()["choices"][0]["message"]["content"]
    assert len(driver.requests) == 1 + 2  # original + 2 retries


def test_json_schema_directive_travels_with_followups(tmp_path):
    driver = StubDriver('{"ok": true}')
    client = _stub_client(tmp_path, driver)
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
    r = client.post("/v1/chat/completions", headers=_auth(), json={
        "messages": [{"role": "user", "content": "check"}],
        "response_format": {"type": "json_schema",
                            "json_schema": {"name": "resp", "schema": schema}}})
    assert r.status_code == 200
    assert json.loads(r.json()["choices"][0]["message"]["content"]) == {"ok": True}
    # The schema summary is part of the prompt sent upstream.
    assert '"type": "object"' in driver.requests[0].prompt


def test_n_gt_1_returns_independent_choices(tmp_path):
    driver = StubDriver("hello")
    client = _stub_client(tmp_path, driver)
    r = client.post("/v1/chat/completions", headers=_auth(), json={
        "messages": [{"role": "user", "content": "hi"}], "n": 3,
        "user": "conv-9"})
    assert r.status_code == 200
    choices = r.json()["choices"]
    assert [c["index"] for c in choices] == [0, 1, 2]
    assert all("hello" in c["message"]["content"] for c in choices)
    # Independent generations: no thread is keyed by the shared user hint.
    assert all(req.conversation_hint is None for req in driver.requests)


def test_stream_n_gt_1_streams_independent_choices(tmp_path):
    driver = StubDriver("hello")
    client = _stub_client(tmp_path, driver)
    with client.stream("POST", "/v1/chat/completions", headers=_auth(), json={
            "messages": [{"role": "user", "content": "hi"}], "n": 2, "stream": True,
            "stream_options": {"include_usage": True}}) as resp:
        assert resp.status_code == 200
        lines = [ln[6:] for ln in resp.iter_lines() if ln.startswith("data: ")
                 and ln != "data: [DONE]"]
    chunks = [json.loads(ln) for ln in lines if ln != ""]
    by_index: dict[int, str] = {}
    finishes = []
    usage_chunks = [c for c in chunks if c["choices"] == []]
    for c in chunks:
        if not c["choices"]:
            continue
        choice = c["choices"][0]
        if choice["delta"].get("content"):
            by_index.setdefault(choice["index"], "")
            by_index[choice["index"]] += choice["delta"]["content"]
        if choice["finish_reason"]:
            finishes.append((choice["index"], choice["finish_reason"]))
    assert by_index == {0: "hello", 1: "hello"}
    assert sorted(finishes) == [(0, "stop"), (1, "stop")]
    assert len(usage_chunks) == 1 and usage_chunks[0]["usage"]["total_tokens"] > 0
    assert usage_chunks[0]["system_fingerprint"] == "muse2api"
    # Ephemeral threads: the pool must see hint=None for both choices.
    assert all(req.conversation_hint is None for req in driver.requests)


def test_envelope_carries_system_fingerprint(tmp_path):
    client = _stub_client(tmp_path, StubDriver("hi"))
    r = client.post("/v1/chat/completions", headers=_auth(), json={
        "messages": [{"role": "user", "content": "hi"}]})
    assert r.json()["system_fingerprint"] == "muse2api"
    with client.stream("POST", "/v1/chat/completions", headers=_auth(), json={
            "messages": [{"role": "user", "content": "hi"}], "stream": True}) as resp:
        lines = [ln[6:] for ln in resp.iter_lines() if ln.startswith("data: ")]
    first = json.loads(lines[0])
    assert first["system_fingerprint"] == "muse2api"


def test_unhandled_errors_use_the_openai_envelope(tmp_path):
    # raise_server_exceptions=False: the handler sends the response, then
    # Starlette still re-raises for the ASGI server to log.
    client = _stub_client(tmp_path, StubDriver("", boom=True),
                          raise_server_exceptions=False)
    r = client.post("/v1/chat/completions", headers=_auth(), json={
        "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 500
    body = r.json()
    assert body["error"]["type"] == "server_error"
    assert "detail" not in body


def test_v1_answers_cors_preflights(tmp_path):
    client = _stub_client(tmp_path, StubDriver("hi"))
    origin = {"Origin": "http://webui.example:3000",
              "Access-Control-Request-Method": "POST",
              "Access-Control-Request-Headers": "authorization"}
    r = client.options("/v1/chat/completions", headers=origin)
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == "*"
    assert "authorization" in r.headers.get("access-control-allow-headers", "")


def test_v1_simple_requests_get_cors_headers(tmp_path):
    client = _stub_client(tmp_path, StubDriver("hi"))
    r = client.post("/v1/chat/completions", headers={**_auth(),
                                                     "Origin": "http://webui.example"})
    assert r.headers["access-control-allow-origin"] == "*"


def test_admin_routes_stay_extension_only(tmp_path):
    client = _stub_client(tmp_path, StubDriver("hi"))
    origin = {"Origin": "http://webui.example",
              "Access-Control-Request-Method": "POST"}
    r = client.options("/admin/accounts", headers=origin)
    assert "access-control-allow-origin" not in r.headers
