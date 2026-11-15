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

    async def chat_stream(self, session_id: str, items: list[dict], *,
                          node_id: str | None = None, hidden_prompt: str | None = None,
                          metadata: dict | None = None) -> dict:
        self.calls.append({"session_id": session_id, "items": items, "node_id": node_id})
        text = items[-1].get("text", "") if items else ""
        message_id = f"m{len(self.calls)}"
        base = len(self._events)
        self._events.extend([
            {"event": "delta.message_start", "seq": base + 1, "payload": {
                "chat_context": {"device_id": node_id}, "message_id": message_id}},
            {"event": "delta.text_append", "seq": base + 2, "payload": {
                "message_id": message_id, "text": f"reply:{text}",
                "session_id": session_id}},
            {"event": "delta.message_done", "seq": base + 3, "payload": {
                "chat_context": {"device_id": node_id}, "status": "completed",
                "transcript": {"messages": [{"content": [
                    {"type": "text", "text": f"reply:{text}"}]}]}}},
        ])
        return {"session_id": session_id, "is_thread": True, "message_id": message_id}

    async def subscribe(self, session_id: str, *, after_stream_seq: int = 0, **kwargs) -> FakeSub:
        self.sub_calls.append((session_id, after_stream_seq))
        return FakeSub(self)

    async def cancel(self, session_id: str, message_id: str | None = None) -> bool:
        self.cancelled = True
        return True


def _driver_with_fake() -> tuple[ProtocolDriver, FakeClient]:
    driver = ProtocolDriver(Settings())
    client = FakeClient()

    async def fake_client(account: Account, state) -> FakeClient:
        return client

    driver._client = fake_client  # type: ignore[method-assign]
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


async def test_protocol_persona_change_starts_a_new_thread():
    driver, client = _driver_with_fake()
    account = Account(id="acc_test")
    await anext(driver.chat_stream(account, _chat(
        "one", [("user", "one")], persona_fingerprint="p1")))
    await anext(driver.chat_stream(account, _chat(
        "full", [("user", "one"), ("assistant", "reply:one"), ("user", "two")],
        persona_fingerprint="p2")))
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
