from __future__ import annotations

import asyncio
import base64

import pytest
from cryptography.exceptions import InvalidTag

from muse2api.accounts.model import Account
from muse2api.config import Settings
from muse2api.drivers.base import ChatRequest, InputImage
from muse2api.drivers.protocol import noise
from muse2api.drivers.protocol.driver import ProtocolDriver
from muse2api.drivers.protocol.media import _media_candidates, resource_kind


def test_cipher_roundtrip_and_reassembler():
    cipher = noise.Cipher(b"k" * 32)
    encrypted = cipher.enc(b"ad", b"hello")
    assert encrypted != b"hello"
    assert noise.Cipher(b"k" * 32).dec(b"ad", encrypted) == b"hello"

    reassembler = noise.Reassembler()
    assert reassembler.push(7, 0, 2, b"ab") is None
    assert reassembler.push(7, 1, 2, b"cd") == b"abcd"


def test_cipher_rejects_wrong_key():
    encrypted = noise.Cipher(b"k" * 32).enc(b"ad", b"hello")
    with pytest.raises(InvalidTag):
        noise.Cipher(b"x" * 32).dec(b"ad", encrypted)


def test_media_candidates_from_presentation():
    payload = {
        "kind": "image",
        "data": {"images": [{
            "path": "sandbox://workspace/imagine_media/demo.webp",
            "mime": "image/webp", "width": 1600, "height": 1600,
        }]},
    }
    out: list = []
    _media_candidates(payload, out)
    assert len(out) == 1
    assert out[0]["resource"]["path"] == "sandbox://workspace/imagine_media/demo.webp"
    assert resource_kind(out[0]["resource"]) == "image"


def test_media_candidates_skip_user_uploads():
    out: list = []
    _media_candidates({"path": "sandbox://workspace/user/files/ref.png"}, out)
    assert out == []


def _chat(prompt: str, turns: list[tuple[str, str]], hint: str | None = "conv-1",
          **kwargs) -> ChatRequest:
    return ChatRequest(prompt=prompt, model="muse-chat", conversation_hint=hint,
                       turns=turns, **kwargs)


class FakeSub:
    def __init__(self, client: FakeClient):
        self.client = client
        self.closed = False

    @property
    def events(self) -> list[dict]:
        return self.client._events

    async def wait_from(self, index: int, timeout: float = 0.5) -> list[dict]:
        await asyncio.sleep(0)
        return self.events[index:]

    def close(self) -> None:
        self.closed = True


class FakeClient:
    alive = True

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.sub_calls: list[tuple[str, int]] = []
        self.cancelled = False
        self._events: list[dict] = []
        self.node_id = "node-fixed-1"
        self.registrations: list[dict] = []
        self.invoke_replies: list[tuple[str, bool, dict]] = []
        self.pending_invoke: dict | None = None
        self.downloaded: list[str] = []

    async def chat_stream(self, session_id: str, items: list[dict], *,
                          node_id: str | None = None, hidden_prompt: str | None = None,
                          metadata: dict | None = None) -> dict:
        self.calls.append({"session_id": session_id, "items": items, "node_id": node_id})
        text = items[-1].get("text", "") if items else ""
        message_id = f"m{len(self.calls)}"
        base = len(self._events)
        events = [
            {"event": "delta.message_start", "seq": base + 1, "payload": {
                "chat_context": {"device_id": node_id}, "message_id": message_id}},
            {"event": "delta.text_append", "seq": base + 2, "payload": {
                "message_id": message_id, "text": f"reply:{text}",
                "session_id": session_id}},
        ]
        if "[[media]]" in text:
            path = "sandbox://workspace/imagine_media/out.png"
            events.append({"event": "delta.presentation", "seq": base + 3, "payload": {
                "kind": "image", "image_path": path,
                "data": {"images": [{"path": path, "label": "out",
                                     "mime": "image/png"}]}}})
        if self.pending_invoke is not None:
            invoke = dict(self.pending_invoke)
            invoke["seq"] = base + 3
            events.append(invoke)
        done = {"event": "delta.message_done", "seq": base + 4, "payload": {
            "chat_context": {"device_id": node_id}, "status": "completed",
            "transcript": {"messages": [{"content": [
                {"type": "text", "text": f"reply:{text}"}]}]}}}
        if "[[media]]" in text:
            done["payload"]["resources"] = [{
                "kind": "citation", "uri": "https://example.com/a",
                "label": "Example A", "metadata": "{\"domain\":\"example.com\"}"}]
        events.append(done)
        self._events.extend(events)
        return {"session_id": session_id, "is_thread": True, "message_id": message_id}

    async def download_bytes(self, resource: dict) -> bytes:
        self.downloaded.append(str(resource.get("path") or ""))
        return b"\x89PNG\r\n\x1a\nfake"

    async def subscribe(self, session_id: str, *, after_stream_seq: int = 0, **kwargs) -> FakeSub:
        self.sub_calls.append((session_id, after_stream_seq))
        return FakeSub(self)

    async def mark_seen(self, thread_id: str) -> None:
        self.seen_calls: list[str] = getattr(self, "seen_calls", [])
        self.seen_calls.append(thread_id)

    async def cancel(self, session_id: str, message_id: str | None = None) -> bool:
        self.cancelled = True
        return True

    async def register_capabilities(self, commands: dict) -> dict:
        self.registrations.append(dict(commands))
        return {"accepted_hatch_app_commands": len(commands), "rejected_ids": []}

    async def reply_invoke(self, invoke_id: str, ok: bool, **kwargs) -> None:
        self.invoke_replies.append((invoke_id, ok, kwargs))


def _driver_with_fake() -> tuple[ProtocolDriver, FakeClient]:
    from muse2api.drivers.protocol.driver import _Conn
    driver = ProtocolDriver(Settings())
    client = FakeClient()

    async def fake_acquire_conn(account: Account, state,
                                preferred) -> _Conn:
        return driver._fake_conn

    driver._fake_conn = _Conn(client=client)
    driver._acquire_conn = fake_acquire_conn  # type: ignore[method-assign]
    return driver, client


async def test_protocol_chat_streams_and_continues():
    driver, client = _driver_with_fake()
    account = Account(id="acc_test")
    first = [d async for d in driver.chat_stream(
        account, _chat("hello", [("user", "hello")]))]
    assert len(first) == 1 and first[0].startswith("reply:") and first[0].endswith("hello")
    assert len(client.calls) == 1
    assert client.calls[0]["items"][-1]["text"].endswith("hello")
    session_id = client.calls[0]["session_id"]

    second = [d async for d in driver.chat_stream(account, _chat(
        "full prompt", [("user", "hello"), ("assistant", "reply:hello"), ("user", "again")]))]
    assert len(second) == 1 and second[0].endswith("again")
    assert client.calls[1]["session_id"] == session_id
    assert client.calls[1]["items"][-1]["text"] == "again"


async def test_protocol_threads_are_isolated_per_hint():
    driver, client = _driver_with_fake()
    account = Account(id="acc_test")
    await anext(driver.chat_stream(account, _chat("one", [("user", "one")], hint="a")))
    await anext(driver.chat_stream(account, _chat("two", [("user", "two")], hint="b")))
    assert client.calls[0]["session_id"] != client.calls[1]["session_id"]


async def test_protocol_system_prompt_change_starts_a_new_thread():
    driver, client = _driver_with_fake()
    account = Account(id="acc_test")
    await anext(driver.chat_stream(account, _chat(
        "one", [("user", "one")], system_fingerprint="p1")))
    await anext(driver.chat_stream(account, _chat(
        "full", [("user", "one"), ("assistant", "reply:one"), ("user", "two")],
        system_fingerprint="p2")))
    assert client.calls[0]["session_id"] != client.calls[1]["session_id"]


async def test_protocol_image_item_uses_base64():
    driver, client = _driver_with_fake()
    account = Account(id="acc_test")
    image = InputImage(data=b"\x89PNG\r\n\x1a\nxx", mime="image/png")
    await anext(driver.chat_stream(account, _chat(
        "look", [("user", "look")], images=[image])))
    item = client.calls[0]["items"][0]
    assert item["type"] == "image"
    assert base64.b64decode(item["data_base64"]).startswith(b"\x89PNG")


async def test_protocol_chat_surfaces_generated_media_and_citations():
    driver, client = _driver_with_fake()
    account = Account(id="acc_test")
    req = _chat("draw [[media]]", [("user", "draw [[media]]")])
    _ = [delta async for delta in driver.chat_stream(account, req)]
    assert [m.kind for m in req.media] == ["image"]
    assert req.media[0].mime == "image/png"
    assert req.media[0].label == "out"
    assert client.downloaded == ["sandbox://workspace/imagine_media/out.png"]
    assert [(c.url, c.title) for c in req.citations] == [
        ("https://example.com/a", "Example A")]


async def test_protocol_health_is_ready():
    driver = ProtocolDriver(Settings())
    health = await driver.health()
    assert health["ok"] is True
    assert health["driver"] == "protocol"


def test_noise_handshake_error_retry_classification():
    from muse2api.drivers.protocol.client import _NoiseHandshakeError

    assert _NoiseHandshakeError("502", status=502).retryable is True
    assert _NoiseHandshakeError(
        "502", marker="fail_ip_registry_not_found").retryable is True
    assert _NoiseHandshakeError("403", status=403).retryable is False


async def test_connect_wakes_vm_when_noise_endpoint_is_unregistered(monkeypatch):
    """Session says RUNNING but the Noise FQDN is missing from Meta's registry
    (502). The client must wake the VM and retry the handshake."""
    from muse2api.drivers.protocol import client as client_module

    session = {"status": "assigned", "endpoint_url": "wss://vm.example/",
               "vm_state": "RUNNING", "vm_id": "vm-1", "vm_type": "hatch_vm"}
    client = client_module.MuseProtocolClient({})
    woke: list[int] = []
    attempts: list[int] = []

    async def fake_http(method, url, body=None, timeout=30.0):
        return dict(session)

    async def fake_wake(sess, *, confirm=20.0):
        woke.append(1)
        return dict(session)

    async def fake_handshake(sess, vm_type):
        attempts.append(1)
        if len(attempts) == 1:
            raise client_module._NoiseHandshakeError(
                "HTTP 502", status=502, marker="fail_ip_registry_not_found")

    async def noop():
        return

    monkeypatch.setattr(client, "_http_json", fake_http)
    monkeypatch.setattr(client, "_wake", fake_wake)
    monkeypatch.setattr(client, "_noise_handshake", fake_handshake)
    monkeypatch.setattr(client, "_receiver", noop)
    monkeypatch.setattr(client, "_heartbeat", noop)
    await client.connect(wake_confirm=0)
    assert len(attempts) == 2
    assert woke == [1]
    await client.close()


async def test_connect_ignores_stale_vm_state(monkeypatch):
    """The live probe reports UNKNOWN while the VM serves turns (2026-11-22:
    the logged-in web app chats with the same workspace that /api/session
    labels UNKNOWN). Connection must proceed without a wake."""
    from muse2api.drivers.protocol import client as client_module

    session = {"status": "assigned", "endpoint_url": "wss://vm.example/",
               "vm_state": "UNKNOWN", "vm_id": "vm-1", "vm_type": "hatch_vm"}
    client = client_module.MuseProtocolClient({})
    woke: list[int] = []
    attempts: list[int] = []

    async def fake_http(method, url, body=None, timeout=30.0):
        return dict(session)

    async def fake_wake(sess, *, confirm=20.0):
        woke.append(1)
        return dict(session)

    async def fake_handshake(sess, vm_type):
        attempts.append(1)

    async def noop():
        return

    monkeypatch.setattr(client, "_http_json", fake_http)
    monkeypatch.setattr(client, "_wake", fake_wake)
    monkeypatch.setattr(client, "_noise_handshake", fake_handshake)
    monkeypatch.setattr(client, "_receiver", noop)
    monkeypatch.setattr(client, "_heartbeat", noop)
    await client.connect(wake_confirm=0)
    assert attempts == [1]
    assert woke == []
    await client.close()


def test_usable_treats_only_disabled_as_hard_down():
    from muse2api.drivers.protocol.client import MuseProtocolClient

    base = {"status": "assigned", "endpoint_url": "wss://vm.example/"}
    for state in ("UNKNOWN", "RUNNING", "SUSPENDED", "", None):
        assert MuseProtocolClient._usable({**base, "vm_state": state}) is True
    assert MuseProtocolClient._usable(
        {**base, "vm_state": "DISABLED"}) is False
    assert MuseProtocolClient._usable(
        {"status": "assigning", "endpoint_url": "wss://vm.example/"}) is False


# ------------------------------------------------------------ native tools
def test_tools_to_commands_maps_openai_schema():
    from muse2api.drivers.protocol.driver import command_id_for_tool, tools_to_commands
    tools = [{"type": "function", "function": {
        "name": "get weather!", "description": "weather lookup",
        "parameters": {"type": "object", "properties": {
            "city": {"type": "string", "description": "city name"},
            "days": {"type": "integer", "enum": [1, 3, 7]}},
            "required": ["city"]}}}]
    commands = tools_to_commands(tools)
    assert list(commands) == ["ui.navigate.get_weather"]
    body = commands["ui.navigate.get_weather"]
    assert body["description"] == "weather lookup"
    assert set(body["required"]) == {"city"}
    assert set(body["optional"]) == {"days"}
    assert body["required"]["city"]["description"] == "city name"
    assert body["optional"]["days"]["enum_values"] == ["1", "3", "7"]
    assert body["timeout_ms"] == 30000
    assert command_id_for_tool("plain-name") == "ui.navigate.plain-name"


async def test_native_tool_invoke_is_captured_and_reencoded():
    driver, client = _driver_with_fake()
    client.pending_invoke = {
        "event": "client.invoke", "payload": {
            "command_id": "ui.navigate.get_weather", "invoke_id": "inv-1",
            "params_json": '{"mode":"perform","city":"Tokyo"}',
        }}
    account = Account(id="acc_test")
    tools = [{"type": "function", "function": {
        "name": "get_weather", "description": "weather",
        "parameters": {"type": "object", "properties": {
            "city": {"type": "string"}}, "required": ["city"]}}}]
    deltas = [d async for d in driver.chat_stream(
        account, _chat("weather?", [("user", "weather?")], tools=tools))]
    assert deltas == ['{"tool": "get_weather", "arguments": {"city": "Tokyo"}}']
    await asyncio.sleep(0)  # let the scheduled invoke ack task run
    # the commands were registered on the connection before the turn
    assert client.registrations
    assert "ui.navigate.get_weather" in client.registrations[0]
    # the invoke was answered so the agent never waits for the timeout
    assert ("inv-1", True) in [(i, ok) for i, ok, _ in client.invoke_replies]
    # Muse's prose stayed out of the tool-call reply
    assert all("reply:" not in d for d in deltas)
    # commands stay registered for the connection lifetime (re-registering the
    # same set later is not picked up by the VM); no empty re-registration
    assert all(reg for reg in client.registrations)


async def test_unmapped_invoke_is_refused_without_capture():
    driver, client = _driver_with_fake()
    client.pending_invoke = {
        "event": "client.invoke", "payload": {
            "command_id": "ui.navigate.get_weather", "invoke_id": "inv-2",
            "params_json": '{"city":"Tokyo"}',
        }}
    account = Account(id="acc_test")
    # no tools in the request: nothing is registered, nothing captured
    deltas = [d async for d in driver.chat_stream(
        account, _chat("hello", [("user", "hello")]))]
    assert len(deltas) == 1 and deltas[0].endswith("hello")
    await asyncio.sleep(0)  # let the scheduled invoke ack task run
    assert client.registrations == []
    # the stray invoke is refused so the agent never waits for a timeout
    assert (False, "not_available") in [
        (ok, (kw.get("error") or {}).get("code"))
        for _, ok, kw in client.invoke_replies]
